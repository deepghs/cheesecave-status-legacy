#!/usr/bin/env python3
"""Build the data of the status page from what Upptime already records.

Upptime commits one line per check ("🟩 Website is up (200 in 211 ms)"), and opens
an issue labelled `status` for every outage. This script reads both and writes
`status.json`, so the page needs no call to the GitHub API in the visitor's browser.

usage: build_status.py --out DIR [--days 90] [--repo OWNER/REPO]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LINE = re.compile(r"^(?P<mark>🟩|🟥|🟨) (?P<name>.+?) (?:is up|is down|performance is degraded)\b")
STATE = {"🟩": "up", "🟥": "down", "🟨": "degraded"}


def site_names(config_text):
    """Names under `sites:` of .upptimerc.yml, in file order (no YAML library needed)."""
    names, inside = [], False
    for line in config_text.splitlines():
        if re.match(r"^sites:\s*$", line):
            inside = True
        elif inside and re.match(r"^\S", line):
            break
        elif inside:
            m = re.match(r"^\s+- name:\s*(.+?)\s*$", line)
            if m:
                names.append(m.group(1).strip("\"'"))
    return names


def parse_checks(log_text):
    """`git log --format=%cI|%s` -> [(datetime, site name, state)], oldest first."""
    checks = []
    for raw in log_text.splitlines():
        stamp, _, subject = raw.partition("|")
        m = LINE.match(subject)
        if not m:
            continue
        when = datetime.fromisoformat(stamp).astimezone(timezone.utc)
        checks.append((when, m["name"], STATE[m["mark"]]))
    checks.sort(key=lambda c: c[0])
    return checks


def day_color(counts):
    """green / yellow / red / none for one day's counts {up, down, degraded}."""
    total = sum(counts.values())
    if total == 0:
        return "none"
    if counts["down"] / total >= 0.05:
        return "red"
    if counts["down"] or counts["degraded"]:
        return "yellow"
    return "green"


def build(names, checks, issues, now, days=90):
    first_day = (now - timedelta(days=days - 1)).date()
    per_site = {n: defaultdict(lambda: {"up": 0, "down": 0, "degraded": 0}) for n in names}
    last = {}
    for when, name, state in checks:
        if name not in per_site:
            continue
        per_site[name][when.date()][state] += 1
        last[name] = (when, state)
    components = []
    for name in names:
        bars, up_total, all_total = [], 0, 0
        for i in range(days):
            day = first_day + timedelta(days=i)
            c = per_site[name].get(day, {"up": 0, "down": 0, "degraded": 0})
            total = sum(c.values())
            up_total += c["up"] + c["degraded"]
            all_total += total
            bars.append({
                "date": day.isoformat(),
                "color": day_color(c),
                "uptime": round(100 * (c["up"] + c["degraded"]) / total, 2) if total else None,
                "down": c["down"],
                "degraded": c["degraded"],
            })
        when, state = last.get(name, (None, "unknown"))
        components.append({
            "name": name,
            "state": state,
            "checked_at": when.isoformat().replace("+00:00", "Z") if when else None,
            "uptime": round(100 * up_total / all_total, 2) if all_total else None,
            "bars": bars,
        })
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "days": days,
        "components": components,
        "incidents": issues,
    }


def read_issues(repo, now, days):
    """Closed and open `status` issues of the last `days` days via the gh CLI."""
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{repo}/issues?labels=status&state=all&since={since}&per_page=100"],
        check=True, capture_output=True, text=True,
    ).stdout
    # --paginate prints one JSON array per page
    items = []
    for chunk in re.split(r"(?<=\])\s*(?=\[)", out.strip()) if out.strip() else []:
        items.extend(json.loads(chunk))
    return [
        {
            "title": i["title"],
            "url": i["html_url"],
            "opened_at": i["created_at"],
            "closed_at": i["closed_at"],
        }
        for i in items
        if "pull_request" not in i and i["created_at"] >= since
    ]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    ap.add_argument("--no-issues", action="store_true")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    now = datetime.now(timezone.utc)
    names = site_names((root / ".upptimerc.yml").read_text(encoding="utf-8"))
    since = (now - timedelta(days=args.days + 1)).strftime("%Y-%m-%d")
    log = subprocess.run(
        ["git", "log", f"--since={since}", "--format=%cI|%s"],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout
    issues = [] if args.no_issues or not args.repo else read_issues(args.repo, now, args.days)
    data = build(names, parse_checks(log), issues, now, args.days)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "status.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in (root / "site").iterdir():
        shutil.copy(f, out / f.name)
    (out / ".nojekyll").write_text("")
    print(f"wrote {out / 'status.json'}: {len(names)} components, {len(issues)} incidents")


if __name__ == "__main__":
    sys.exit(main())
