"""Probe a CheeseCave instance and turn the checks into the data of its status page.

Nothing here knows a particular instance: every address comes from status.config.json.
Standard library only, so a workflow needs no install step.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rollup

DEFAULTS = {
    "interval_seconds": 300,   # time between two rounds of checks
    "timeout_seconds": 20,     # one request
    "degraded_ms": 8000,       # slower than this counts as degraded (a component may set its own)
    "raw_days": 14,            # raw checks are kept this long; hourly rows are kept for good
    "confirm_failures": 2,     # consecutive failed rounds before an outage is declared
}
DEFAULT_GROUP = "Services"


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

def http_fetch(url, headers, timeout, max_bytes, method="GET", data=None):
    """Request `url` (redirects are followed): (status, first max_bytes of the body, seconds to the headers, seconds to read the body)."""
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "cheesecave-status/1", **headers})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            got_headers = time.monotonic()
            body = resp.read(max_bytes)
            return resp.status, body, got_headers - started, time.monotonic() - got_headers
    except urllib.error.HTTPError as err:
        return err.code, b"", time.monotonic() - started, 0.0


def probe(cfg, comp, fetch=http_fetch, clock=time.monotonic):
    """-> (state, ms, ttfb_ms, MB/s); state is up, degraded or down. The last two are None when not measured."""
    expect = comp.get("expect_status", list(range(200, 300)))
    body_data = comp["body"].encode() if comp.get("body") is not None else None
    start = clock()
    try:
        res = fetch(component_url(cfg, comp), comp.get("headers", {}), cfg["timeout_seconds"],
                    comp.get("max_bytes", 1_000_000), comp.get("method", "GET"), body_data)
    except Exception:
        return "down", None, None, None
    ms = int((clock() - start) * 1000)
    status, body = res[0], res[1]
    ttfb = int(res[2] * 1000) if len(res) > 2 else None
    read_seconds = res[3] if len(res) > 3 else None
    speed = None
    if comp.get("speed") and body and read_seconds is not None:
        speed = round(len(body) / 1_000_000 / max(read_seconds, 0.001), 2)
    if status not in expect:
        return "down", ms, ttfb, speed
    want = comp.get("expect_json")
    if want is not None:
        try:
            data = json.loads(body)
        except ValueError:
            return "down", ms, ttfb, speed
        if not isinstance(data, dict) or any(data.get(k) != v for k, v in want.items()):
            return "down", ms, ttfb, speed
    text = comp.get("expect_text")
    if text is not None and text not in body.decode("utf-8", "replace"):
        return "down", ms, ttfb, speed
    slow = ms > comp.get("degraded_ms", cfg["degraded_ms"])
    if speed is not None and "min_speed" in comp and speed < comp["min_speed"]:
        slow = True
    return ("degraded" if slow else "up"), ms, ttfb, speed


def check_all(cfg, now, fetch=http_fetch):
    """One round: {"t": time, "c": {component: [state, ms, ttfb_ms, MB/s]}}; components are probed in parallel."""
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
    """Raw records of the last `days` days, oldest first."""
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


def apply_actions(incidents, actions, now, gh, notify=None):
    """Open or close incidents in `incidents` (a list of dicts); `gh` creates and closes the GitHub issues,
    `notify(text)` (optional) tells the maintainers."""
    for kind, name in actions:
        if kind == "open":
            title = f"{name} is down"
            try:
                number, url = gh.open_issue(title, f"Checks of **{name}** have been failing since {iso(now)}. This issue is opened and closed automatically.")
            except Exception:
                number, url = None, None  # an outage must still reach the page when GitHub refuses the issue
            incidents.append({"component": name, "title": title, "url": url, "issue": number,
                              "opened_at": iso(now), "closed_at": None})
            _tell(notify, f"🛑 {title} (since {iso(now)})" + (f" {url}" if url else ""))
        else:
            for inc in incidents:
                if inc["component"] == name and inc["closed_at"] is None:
                    inc["closed_at"] = iso(now)
                    _tell(notify, f"✅ {name} is reachable again ({iso(now)})")
                    if inc["issue"]:
                        try:
                            gh.close_issue(inc["issue"], f"{name} is reachable again ({iso(now)}).")
                        except Exception:
                            pass


def _tell(notify, text):
    if notify:
        try:
            notify(text)
        except Exception:
            pass  # a broken webhook must never stop the checks


def make_notifier(url=None, style=None, post=None):
    """A function that POSTs a text to a chat webhook (NOTIFY_WEBHOOK / NOTIFY_STYLE), or None without a URL."""
    url = url if url is not None else os.environ.get("NOTIFY_WEBHOOK")
    if not url:
        return None
    style = (style or os.environ.get("NOTIFY_STYLE") or "slack").lower()
    shape = {"slack": lambda t: {"text": t}, "discord": lambda t: {"content": t},
             "lark": lambda t: {"msg_type": "text", "content": {"text": t}}}[style]

    def default_post(address, payload):
        req = urllib.request.Request(address, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "cheesecave-status/1"})
        urllib.request.urlopen(req, timeout=15).read()

    return lambda text: (post or default_post)(url, shape(text))


def load_incidents(data_dir):
    path = Path(data_dir) / "incidents.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def save_incidents(data_dir, incidents, now, days):
    cutoff = iso(now - timedelta(days=days))
    kept = [i for i in incidents if i["closed_at"] is None or i["closed_at"] >= cutoff]
    (Path(data_dir) / "incidents.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    return kept


# ---------------------------------------------------------------- the data of the page

def groups_of(cfg):
    """[{"name", "components": [names]}] in the order of the config (components without a group go to 'Services')."""
    order, members = [], {}
    for comp in cfg["components"]:
        g = comp.get("group", DEFAULT_GROUP)
        if g not in members:
            order.append(g)
            members[g] = []
        members[g].append(comp["name"])
    return [{"name": g, "components": members[g]} for g in order]


def build_status(cfg, records, views, incidents, now):
    """status.json: what the page needs before it has loaded any view (states, summary figures, incidents)."""
    confirm = cfg["confirm_failures"]
    components = []
    for comp in cfg["components"]:
        name = comp["name"]
        latest = next((r for r in reversed(records) if name in r["c"]), None)
        entry = latest["c"][name] if latest else None
        components.append({
            "name": name,
            "group": comp.get("group", DEFAULT_GROUP),
            "state": current_state(component_states(records, name), confirm),
            "checked_at": latest["t"] if latest else None,
            "ms": entry[1] if entry else None,
            "ttfb_ms": entry[2] if entry and len(entry) > 2 else None,
            "speed": entry[3] if entry and len(entry) > 3 else None,
            "has_speed": bool(comp.get("speed")),
            "uptime": {rng: rollup.uptime(views[rng]["components"][name]) for rng in rollup.RANGES},
        })
    return {
        "generated_at": iso(now),
        "interval_seconds": cfg["interval_seconds"],
        "ranges": list(rollup.RANGES),
        "instance": {"name": cfg["name"], "home_url": cfg["base_url"], **({"description": cfg["description"]} if cfg.get("description") else {})},
        "groups": groups_of(cfg),
        "components": components,
        "incidents": [{k: i[k] for k in ("component", "title", "url", "opened_at", "closed_at")} for i in incidents],
    }


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def tick(cfg, data_dir, now, gh, fetch=http_fetch, notify=None):
    """One round: probe, record, roll up, update incidents, write status.json and the views. Returns status.json's content."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    append_record(data_dir, check_all(cfg, now, fetch))
    prune(data_dir, now, cfg["raw_days"])
    records = read_records(data_dir, now, cfg["raw_days"])
    names = [c["name"] for c in cfg["components"]]

    hourly = rollup.load_hourly(data_dir)
    fresh = rollup.hourly_from_records(records, names)
    for hour, by in fresh.items():
        hourly.setdefault(hour, {}).update(by)
    rollup.save_hourly(data_dir, hourly, fresh)
    views = {rng: rollup.build_view(rng, names, now, records, hourly) for rng in rollup.RANGES}
    for rng, view in views.items():
        write_json(data_dir / "view" / f"{rng}.json", view)

    confirm = cfg["confirm_failures"]
    incidents = load_incidents(data_dir)
    open_now = {i["component"]: i for i in incidents if i["closed_at"] is None}
    recent = {n: component_states(records, n)[-confirm:] for n in names}
    apply_actions(incidents, incident_actions(open_now, recent, confirm), now, gh, notify)
    incidents = save_incidents(data_dir, incidents, now, 365)
    status = build_status(cfg, records, views, incidents, now)
    write_json(data_dir / "status.json", status)
    return status


def pages_config(cfg, repo):
    """What the page needs to know about the instance and where the data lives (written to config.json)."""
    return {
        "name": cfg["name"],
        "home_url": cfg["base_url"],
        "data_base": f"https://raw.githubusercontent.com/{repo}/status/",
        "fallback_base": "",
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
