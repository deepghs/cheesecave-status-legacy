"""Probe a CheeseCave instance and turn the checks into the data of its status page.

Nothing here knows a particular instance: every address comes from status.config.json.
Standard library only, so a workflow needs no install step.
"""
import json
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULTS = {
    "interval_seconds": 300,   # time between two rounds of checks
    "timeout_seconds": 20,     # one request
    "degraded_ms": 8000,       # slower than this counts as degraded
    "history_days": 90,
    "confirm_failures": 2,     # consecutive failed rounds before an outage is declared
}


def iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# ---------------------------------------------------------------- configuration

def load_config(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in ("name", "base_url", "components"):
        if not raw.get(key):
            raise ValueError(f"{path}: '{key}' is required")
    cfg = {**DEFAULTS, **raw}
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    names = [c.get("name") for c in cfg["components"]]
    if not all(names) or len(set(names)) != len(names):
        raise ValueError(f"{path}: every component needs a unique name")
    for comp in cfg["components"]:
        if ("path" in comp) == ("url" in comp):
            raise ValueError(f"{path}: component '{comp['name']}' needs exactly one of 'path' and 'url'")
    return cfg


def component_url(cfg, comp):
    return comp["url"] if "url" in comp else cfg["base_url"] + comp["path"]


# ---------------------------------------------------------------- probing

def http_fetch(url, headers, timeout, max_bytes):
    """GET `url` (redirects are followed) and return (status, first max_bytes of the body)."""
    req = urllib.request.Request(url, headers={"User-Agent": "cheesecave-status/1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(max_bytes)
    except urllib.error.HTTPError as err:
        return err.code, b""


def probe(cfg, comp, fetch=http_fetch, clock=time.monotonic):
    """-> (state, milliseconds or None); state is up, degraded or down."""
    expect = comp.get("expect_status", list(range(200, 300)))
    start = clock()
    try:
        status, body = fetch(component_url(cfg, comp), comp.get("headers", {}),
                             cfg["timeout_seconds"], comp.get("max_bytes", 1_000_000))
    except Exception:
        return "down", None
    ms = int((clock() - start) * 1000)
    if status not in expect:
        return "down", ms
    want = comp.get("expect_json")
    if want is not None:
        try:
            data = json.loads(body)
        except ValueError:
            return "down", ms
        if not isinstance(data, dict) or any(data.get(k) != v for k, v in want.items()):
            return "down", ms
    return ("degraded" if ms > cfg["degraded_ms"] else "up"), ms


def check_all(cfg, now, fetch=http_fetch):
    """One round: {"t": time, "c": {component: [state, ms]}}; components are probed in parallel."""
    comps = cfg["components"]
    with ThreadPoolExecutor(max_workers=len(comps)) as pool:
        results = list(pool.map(lambda c: probe(cfg, c, fetch), comps))
    return {"t": iso(now), "c": {c["name"]: list(r) for c, r in zip(comps, results)}}


# ---------------------------------------------------------------- records on disk

def append_record(data_dir, record):
    day = record["t"][:10]
    path = Path(data_dir) / "checks" / f"{day}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_records(data_dir, now, days):
    """Records of the last `days` days (plus the day before, for the window edge), oldest first."""
    first = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    records = []
    for path in sorted((Path(data_dir) / "checks").glob("*.jsonl")):
        if path.stem < first:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                pass  # a torn last line must not lose the whole day
    records.sort(key=lambda r: r["t"])
    return records


def prune(data_dir, now, days):
    cutoff = (now - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    for path in (Path(data_dir) / "checks").glob("*.jsonl"):
        if path.stem < cutoff:
            path.unlink()


# ---------------------------------------------------------------- states and incidents

def day_color(counts):
    total = sum(counts.values())
    if total == 0:
        return "none"
    if counts["down"] / total >= 0.05:
        return "red"
    if counts["down"] or counts["degraded"]:
        return "yellow"
    return "green"


def current_state(states, confirm):
    """The last state; a single failed round is only 'degraded' until `confirm` rounds in a row failed."""
    if not states:
        return "unknown"
    if states[-1] == "down" and not (len(states) >= confirm and all(s == "down" for s in states[-confirm:])):
        return "degraded"
    return states[-1]


def component_states(records, name):
    return [r["c"][name][0] for r in records if name in r["c"]]


def incident_actions(open_by_component, states_by_component, confirm):
    actions = []
    for name, states in states_by_component.items():
        if name not in open_by_component:
            if len(states) >= confirm and all(s == "down" for s in states[-confirm:]):
                actions.append(("open", name))
        elif states and states[-1] != "down":
            actions.append(("close", name))
    return actions


def apply_actions(incidents, actions, now, gh):
    """Open or close incidents in `incidents` (a list of dicts); `gh` creates and closes the GitHub issues."""
    for kind, name in actions:
        if kind == "open":
            title = f"{name} is down"
            try:
                number, url = gh.open_issue(title, f"Checks of **{name}** have been failing since {iso(now)}. This issue is opened and closed automatically.")
            except Exception:
                number, url = None, None  # an outage must still reach the page when GitHub refuses the issue
            incidents.append({"component": name, "title": title, "url": url, "issue": number,
                              "opened_at": iso(now), "closed_at": None})
        else:
            for inc in incidents:
                if inc["component"] == name and inc["closed_at"] is None:
                    inc["closed_at"] = iso(now)
                    if inc["issue"]:
                        try:
                            gh.close_issue(inc["issue"], f"{name} is reachable again ({iso(now)}).")
                        except Exception:
                            pass


def load_incidents(data_dir):
    path = Path(data_dir) / "incidents.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_incidents(data_dir, incidents, now, days):
    cutoff = iso(now - timedelta(days=days))
    kept = [i for i in incidents if i["closed_at"] is None or i["closed_at"] >= cutoff]
    (Path(data_dir) / "incidents.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    return kept


# ---------------------------------------------------------------- the data of the page

def build(cfg, records, incidents, now):
    days = cfg["history_days"]
    first_day = (now - timedelta(days=days - 1)).date()
    components = []
    for comp in cfg["components"]:
        name = comp["name"]
        per_day, up_total, all_total, last_at = {}, 0, 0, None
        for r in records:
            if name not in r["c"]:
                continue
            state = r["c"][name][0]
            counts = per_day.setdefault(parse_iso(r["t"]).date(), {"up": 0, "down": 0, "degraded": 0})
            counts[state] += 1
            last_at = r["t"]
        bars = []
        for i in range(days):
            day = first_day + timedelta(days=i)
            c = per_day.get(day, {"up": 0, "down": 0, "degraded": 0})
            total = sum(c.values())
            up_total += c["up"] + c["degraded"]
            all_total += total
            bars.append({"date": day.isoformat(), "color": day_color(c),
                         "uptime": round(100 * (c["up"] + c["degraded"]) / total, 2) if total else None,
                         "down": c["down"], "degraded": c["degraded"]})
        components.append({
            "name": name,
            "state": current_state(component_states(records, name), cfg["confirm_failures"]),
            "checked_at": last_at,
            "uptime": round(100 * up_total / all_total, 2) if all_total else None,
            "bars": bars,
        })
    return {
        "generated_at": iso(now),
        "interval_seconds": cfg["interval_seconds"],
        "days": days,
        "instance": {"name": cfg["name"], "home_url": cfg["base_url"]},
        "components": components,
        "incidents": [{k: i[k] for k in ("component", "title", "url", "opened_at", "closed_at")} for i in incidents],
    }


def tick(cfg, data_dir, now, gh, fetch=http_fetch):
    """One round: probe, record, update incidents, write status.json. Returns the status dict."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    append_record(data_dir, check_all(cfg, now, fetch))
    prune(data_dir, now, cfg["history_days"])
    records = read_records(data_dir, now, cfg["history_days"])
    confirm = cfg["confirm_failures"]
    incidents = load_incidents(data_dir)
    open_now = {i["component"]: i for i in incidents if i["closed_at"] is None}
    recent = {c["name"]: component_states(records, c["name"])[-confirm:] for c in cfg["components"]}
    apply_actions(incidents, incident_actions(open_now, recent, confirm), now, gh)
    incidents = save_incidents(data_dir, incidents, now, cfg["history_days"])
    status = build(cfg, records, incidents, now)
    (data_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return status


def pages_config(cfg, repo):
    """What the page needs to know about the instance and where the data lives (written to config.json)."""
    return {
        "name": cfg["name"],
        "home_url": cfg["base_url"],
        "data_url": f"https://raw.githubusercontent.com/{repo}/status/status.json",
        "fallback_url": "status.json",
        "refresh_seconds": 60,
    }


# ---------------------------------------------------------------- the `status` branch

def _git(args, cwd, header=None):
    cmd = ["git"] + (["-c", f"http.extraheader={header}"] if header else []) + args
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def restore(data_dir, remote, header=None, branch="status"):
    """Bring the files of the data branch into data_dir; an empty directory when it does not exist yet."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], data_dir)
    try:
        _git(["fetch", "-q", "--depth", "1", remote, branch], data_dir, header)
    except subprocess.CalledProcessError:
        return False
    _git(["checkout", "-q", "-f", "FETCH_HEAD"], data_dir)
    return True


def publish(data_dir, remote, message, header=None, branch="status"):
    """Replace the data branch with one fresh commit of data_dir (history is not kept: the files are the history)."""
    _git(["add", "-A"], data_dir)
    tree = _git(["write-tree"], data_dir)
    commit = _git(["-c", "user.name=cheesecave-status", "-c", "user.email=status@users.noreply.github.com",
                   "commit-tree", tree, "-m", message], data_dir)
    _git(["push", "-q", "--force", remote, f"{commit}:refs/heads/{branch}"], data_dir, header)
    return commit
