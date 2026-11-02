"""python3 -I scripts/test_statuslib.py"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import statuslib as s  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
CFG = {**s.DEFAULTS, "name": "Hub", "base_url": "https://hub.example", "history_days": 3,
       "components": [{"name": "Website", "path": "/"},
                      {"name": "API", "path": "/api/version", "expect_json": {"api": "cheesecave"}},
                      {"name": "File", "url": "https://files.example/x", "expect_status": [200, 206],
                       "headers": {"Range": "bytes=0-9"}}]}


def fetcher(table):
    """table: url -> (status, body) or an Exception"""
    def fetch(url, headers, timeout, max_bytes):
        r = table[url]
        if isinstance(r, Exception):
            raise r
        return r
    return fetch


class FakeGh:
    def __init__(self, fail=False):
        self.opened, self.closed, self.fail = [], [], fail

    def open_issue(self, title, body):
        if self.fail:
            raise RuntimeError("no")
        self.opened.append(title)
        return len(self.opened), f"https://x/issues/{len(self.opened)}"

    def close_issue(self, number, comment):
        if self.fail:
            raise RuntimeError("no")
        self.closed.append(number)


class Config(unittest.TestCase):
    def write(self, obj):
        d = tempfile.mkdtemp()
        p = Path(d) / "c.json"
        p.write_text(json.dumps(obj))
        return p

    def test_defaults_and_trailing_slash(self):
        cfg = s.load_config(self.write({"name": "n", "base_url": "https://h/", "components": [{"name": "a", "path": "/"}]}))
        self.assertEqual(cfg["base_url"], "https://h")
        self.assertEqual(cfg["interval_seconds"], 300)

    def test_rejects_missing_duplicate_and_ambiguous(self):
        ok = {"name": "n", "base_url": "https://h", "components": [{"name": "a", "path": "/"}]}
        for bad in ({**ok, "name": ""}, {**ok, "components": []},
                    {**ok, "components": [{"name": "a", "path": "/"}, {"name": "a", "path": "/"}]},
                    {**ok, "components": [{"name": "a"}]},
                    {**ok, "components": [{"name": "a", "path": "/", "url": "https://x"}]}):
            with self.assertRaises(ValueError):
                s.load_config(self.write(bad))

    def test_component_url(self):
        self.assertEqual(s.component_url(CFG, CFG["components"][0]), "https://hub.example/")
        self.assertEqual(s.component_url(CFG, CFG["components"][2]), "https://files.example/x")


class Probe(unittest.TestCase):
    def run_probe(self, comp, result, ms=10):
        ticks = iter([0.0, ms / 1000])
        return s.probe(CFG, comp, fetcher({s.component_url(CFG, comp): result}), clock=lambda: next(ticks))

    def test_up_degraded_down(self):
        web = CFG["components"][0]
        self.assertEqual(self.run_probe(web, (200, b"")), ("up", 10))
        self.assertEqual(self.run_probe(web, (200, b""), ms=9000), ("degraded", 9000))
        self.assertEqual(self.run_probe(web, (503, b"")), ("down", 10))

    def test_network_error_is_down_without_time(self):
        self.assertEqual(s.probe(CFG, CFG["components"][0], fetcher({"https://hub.example/": OSError("x")})), ("down", None))

    def test_expected_status_list(self):
        f = CFG["components"][2]
        self.assertEqual(self.run_probe(f, (206, b""))[0], "up")
        self.assertEqual(self.run_probe(f, (302, b""))[0], "down")

    def test_expect_json(self):
        api = CFG["components"][1]
        self.assertEqual(self.run_probe(api, (200, b'{"api":"cheesecave","v":1}'))[0], "up")
        self.assertEqual(self.run_probe(api, (200, b'{"api":"other"}'))[0], "down")
        self.assertEqual(self.run_probe(api, (200, b"<html>"))[0], "down")
        self.assertEqual(self.run_probe(api, (200, b"[1]"))[0], "down")

    def test_check_all_names_every_component(self):
        table = {"https://hub.example/": (200, b""), "https://hub.example/api/version": (200, b'{"api":"cheesecave"}'),
                 "https://files.example/x": OSError("x")}
        rec = s.check_all(CFG, NOW, fetcher(table))
        self.assertEqual(rec["t"], "2026-11-02T12:00:00Z")
        self.assertEqual([rec["c"][n][0] for n in ("Website", "API", "File")], ["up", "up", "down"])


class Records(unittest.TestCase):
    def test_append_read_prune_and_torn_line(self):
        d = Path(tempfile.mkdtemp())
        for day in ("2026-10-20", "2026-11-01", "2026-11-02"):
            s.append_record(d, {"t": f"{day}T10:00:00Z", "c": {"A": ["up", 1]}})
        with (d / "checks" / "2026-11-02.jsonl").open("a") as f:
            f.write('{"t": "2026-11-02T11')  # torn write
        got = s.read_records(d, NOW, 3)
        self.assertEqual([r["t"][:10] for r in got], ["2026-11-01", "2026-11-02"])
        s.prune(d, NOW, 3)
        self.assertEqual(sorted(p.stem for p in (d / "checks").glob("*.jsonl")), ["2026-11-01", "2026-11-02"])


class States(unittest.TestCase):
    def test_day_color(self):
        z = {"up": 0, "down": 0, "degraded": 0}
        self.assertEqual(s.day_color(z), "none")
        self.assertEqual(s.day_color({**z, "up": 9}), "green")
        self.assertEqual(s.day_color({**z, "up": 99, "down": 1}), "yellow")
        self.assertEqual(s.day_color({**z, "up": 99, "degraded": 1}), "yellow")
        self.assertEqual(s.day_color({**z, "up": 19, "down": 1}), "red")

    def test_current_state_needs_confirmation(self):
        self.assertEqual(s.current_state([], 2), "unknown")
        self.assertEqual(s.current_state(["up"], 2), "up")
        self.assertEqual(s.current_state(["up", "down"], 2), "degraded")
        self.assertEqual(s.current_state(["down"], 2), "degraded")
        self.assertEqual(s.current_state(["up", "down", "down"], 2), "down")

    def test_incident_actions(self):
        self.assertEqual(s.incident_actions({}, {"A": ["up", "down", "down"]}, 2), [("open", "A")])
        self.assertEqual(s.incident_actions({}, {"A": ["down"]}, 2), [])
        self.assertEqual(s.incident_actions({"A": {}}, {"A": ["down", "down"]}, 2), [])
        self.assertEqual(s.incident_actions({"A": {}}, {"A": ["down", "up"]}, 2), [("close", "A")])
        self.assertEqual(s.incident_actions({"A": {}}, {"A": []}, 2), [])

    def test_apply_actions_opens_and_closes(self):
        gh, inc = FakeGh(), []
        s.apply_actions(inc, [("open", "A")], NOW, gh)
        self.assertEqual((inc[0]["issue"], inc[0]["closed_at"], gh.opened), (1, None, ["A is down"]))
        s.apply_actions(inc, [("close", "A")], NOW, gh)
        self.assertEqual((inc[0]["closed_at"], gh.closed), ("2026-11-02T12:00:00Z", [1]))

    def test_github_refusing_does_not_lose_the_incident(self):
        gh, inc = FakeGh(fail=True), []
        s.apply_actions(inc, [("open", "A")], NOW, gh)
        self.assertIsNone(inc[0]["issue"])
        s.apply_actions(inc, [("close", "A")], NOW, gh)
        self.assertEqual(inc[0]["closed_at"], "2026-11-02T12:00:00Z")

    def test_save_incidents_drops_old_closed_ones(self):
        d = Path(tempfile.mkdtemp())
        old = {"component": "A", "title": "t", "url": None, "issue": None, "opened_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-02T00:00:00Z"}
        live = {**old, "closed_at": None}
        self.assertEqual(s.save_incidents(d, [old, live], NOW, 3), [live])
        self.assertEqual(s.load_incidents(d), [live])
        self.assertEqual(s.load_incidents(Path(tempfile.mkdtemp())), [])


class Build(unittest.TestCase):
    def rec(self, when, **states):
        return {"t": when, "c": {k: [v, 5] for k, v in states.items()}}

    def test_window_bars_uptime_and_checked_at(self):
        recs = [self.rec("2026-11-02T09:00:00Z", Website="up"), self.rec("2026-11-02T10:00:00Z", Website="down"),
                self.rec("2026-11-02T11:00:00Z", Website="up")]
        data = s.build(CFG, recs, [], NOW)
        web = data["components"][0]
        self.assertEqual([b["date"] for b in web["bars"]], ["2026-10-31", "2026-11-01", "2026-11-02"])
        self.assertEqual([b["color"] for b in web["bars"]], ["none", "none", "red"])
        self.assertEqual((web["uptime"], web["state"], web["checked_at"]), (66.67, "up", "2026-11-02T11:00:00Z"))
        self.assertEqual(data["instance"], {"name": "Hub", "home_url": "https://hub.example"})
        self.assertEqual(data["components"][1]["state"], "unknown")

    def test_incidents_are_listed_without_internal_fields(self):
        inc = [{"component": "A", "title": "A is down", "url": None, "issue": 3, "opened_at": "x", "closed_at": None}]
        self.assertNotIn("issue", s.build(CFG, [], inc, NOW)["incidents"][0])


class Tick(unittest.TestCase):
    OK = {"https://hub.example/": (200, b""), "https://hub.example/api/version": (200, b'{"api":"cheesecave"}'),
          "https://files.example/x": (206, b"")}

    def test_outage_opens_after_two_rounds_and_closes_on_recovery(self):
        d, gh = Path(tempfile.mkdtemp()), FakeGh()
        down = {**self.OK, "https://hub.example/": (500, b"")}
        s.tick(CFG, d, NOW, gh, fetcher(self.OK))
        st = s.tick(CFG, d, NOW + timedelta(minutes=5), gh, fetcher(down))
        self.assertEqual((st["components"][0]["state"], gh.opened), ("degraded", []))
        st = s.tick(CFG, d, NOW + timedelta(minutes=10), gh, fetcher(down))
        self.assertEqual((st["components"][0]["state"], gh.opened), ("down", ["Website is down"]))
        st = s.tick(CFG, d, NOW + timedelta(minutes=15), gh, fetcher(self.OK))
        self.assertEqual((st["components"][0]["state"], gh.closed), ("up", [1]))
        self.assertEqual(st["incidents"][0]["closed_at"], "2026-11-02T12:15:00Z")
        self.assertEqual(json.loads((d / "status.json").read_text())["generated_at"], "2026-11-02T12:15:00Z")


class PagesConfig(unittest.TestCase):
    def test_points_the_page_at_the_status_branch_of_the_repository(self):
        c = s.pages_config(CFG, "acme/status")
        self.assertEqual(c["data_url"], "https://raw.githubusercontent.com/acme/status/status/status.json")
        self.assertEqual((c["name"], c["home_url"], c["fallback_url"]), ("Hub", "https://hub.example", "status.json"))


class Branch(unittest.TestCase):
    def test_publish_replaces_history_and_restore_brings_files_back(self):
        remote = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        a = Path(tempfile.mkdtemp())
        self.assertFalse(s.restore(a, remote))            # branch does not exist yet
        (a / "status.json").write_text("{}")
        s.publish(a, remote, "one")
        (a / "status.json").write_text('{"n":2}')
        s.publish(a, remote, "two")
        count = subprocess.run(["git", "--git-dir", remote, "rev-list", "--count", "status"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(count, "1")                      # one commit, not a growing history
        b = Path(tempfile.mkdtemp())
        self.assertTrue(s.restore(b, remote))
        self.assertEqual((b / "status.json").read_text(), '{"n":2}')


if __name__ == "__main__":
    unittest.main()
