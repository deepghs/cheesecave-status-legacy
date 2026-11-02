#!/usr/bin/env python3
"""Check the instance in a loop and publish the data to the `status` branch.

usage: monitor.py [--config status.config.json] [--minutes 340] [--data-dir DIR] [--remote URL]

One run of the Monitor workflow is one long job: GitHub's own cron is too coarse (it fired every ~3 hours
here), so the job is the scheduler. It stops by itself before the 6 hour limit and the workflow starts the
next copy.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also under `python3 -I`
import statuslib as s  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class GhCli:
    """Opens and closes the GitHub issues that announce an outage (the `status` label is created on first use)."""

    def __init__(self, repo):
        self.repo = repo

    def _api(self, *args):
        return subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True).stdout

    def open_issue(self, title, body):
        out = json.loads(self._api(f"repos/{self.repo}/issues", "-f", f"title={title}", "-f", f"body={body}", "-f", "labels[]=status"))
        return out["number"], out["html_url"]

    def close_issue(self, number, comment):
        self._api(f"repos/{self.repo}/issues/{number}/comments", "-f", f"body={comment}")
        self._api("-X", "PATCH", f"repos/{self.repo}/issues/{number}", "-f", "state=closed")


def describe(status):
    return " ".join(f"{c['name']}={c['state']}" for c in status["components"])


def why(err):
    """An error as one safe line: a failed git command's own arguments can carry the credential."""
    if isinstance(err, subprocess.CalledProcessError):
        return f"{Path(err.cmd[0]).name} failed: {(err.stderr or '').strip()[:200]}"
    return f"{type(err).__name__}: {str(err)[:200]}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "status.config.json"))
    ap.add_argument("--minutes", type=float, default=340)
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--remote")
    args = ap.parse_args(argv)

    cfg = s.load_config(args.config)
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    remote = args.remote or f"https://github.com/{repo}.git"
    token = os.environ.get("GITHUB_TOKEN")
    header = "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode() if token else None
    gh = GhCli(repo)
    notify = s.make_notifier()

    try:
        print("restored the data branch" if s.restore(args.data_dir, remote, header) else "no data branch yet, starting empty", flush=True)
    except Exception as err:
        print("restore failed:", why(err), flush=True)
        return 1
    deadline = time.time() + args.minutes * 60
    while True:
        started = time.time()
        now = datetime.now(timezone.utc)
        try:
            status = s.tick(cfg, args.data_dir, now, gh, notify=notify)
            print(s.iso(now), describe(status), flush=True)
        except Exception as err:
            print(s.iso(now), "check failed:", why(err), flush=True)
        try:
            s.publish(args.data_dir, remote, f"checks {s.iso(now)}", header)
        except Exception as err:
            print(s.iso(now), "publish failed (will retry next round):", why(err), flush=True)
        wake = started + cfg["interval_seconds"]
        if wake >= deadline:
            return 0
        time.sleep(max(0, wake - time.time()))


if __name__ == "__main__":
    sys.exit(main())
