"""python3 -I scripts/test_statuslib.py"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rollup  # noqa: E402
import statuslib as s  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
CFG = {**s.DEFAULTS, "name": "Hub", "base_url": "https://hub.example",
       "components": [{"name": "Website", "path": "/", "group": "Web"},
                      {"name": "API", "path": "/api/version", "expect_json": {"api": "cheesecave"}, "group": "API"},
                      {"name": "File", "url": "https://files.example/x", "expect_status": [200, 206], "group": "Downloads",
                       "headers": {"Range": "bytes=0-9"}, "speed": True, "min_speed": 1.0, "degraded_ms": 3000},
                      {"name": "Ungrouped", "path": "/u"}]}


def fetcher(table):
    """table: url -> (status, body[, ttfb, read]) or an Exception"""
    def fetch(url, headers, timeout, max_bytes, method="GET", data=None):
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
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(obj))
        return p

    def test_defaults_and_trailing_slash(self):
        cfg = s.load_config(self.write({"name": "n", "base_url": "https://h/", "components": [{"name": "a", "path": "/"}]}))
        self.assertEqual((cfg["base_url"], cfg["interval_seconds"], cfg["raw_days"]), ("https://h", 300, 14))

    def test_rejects_missing_duplicate_and_ambiguous(self):
        ok = {"name": "n", "base_url": "https://h", "components": [{"name": "a", "path": "/"}]}
        for bad in ({**ok, "name": ""}, {**ok, "components": []},
                    {**ok, "components": [{"name": "a", "path": "/"}, {"name": "a", "path": "/"}]},
                    {**ok, "components": [{"name": "a"}]},
                    {**ok, "components": [{"name": "a", "path": "/", "url": "https://x"}]}):
            with self.assertRaises(ValueError):
                s.load_config(self.write(bad))

    def test_component_url_and_groups_keep_config_order(self):
        self.assertEqual(s.component_url(CFG, CFG["components"][0]), "https://hub.example/")
        self.assertEqual(s.component_url(CFG, CFG["components"][2]), "https://files.example/x")
        self.assertEqual(s.groups_of(CFG), [{"name": "Web", "components": ["Website"]}, {"name": "API", "components": ["API"]},
                                           {"name": "Downloads", "components": ["File"]}, {"name": "Services", "components": ["Ungrouped"]}])


class Probe(unittest.TestCase):
    def run_probe(self, comp, result, ms=10):
        ticks = iter([0.0, ms / 1000])
        return s.probe(CFG, comp, fetcher({s.component_url(CFG, comp): result}), clock=lambda: next(ticks))

    def test_up_degraded_down(self):
        web = CFG["components"][0]
        self.assertEqual(self.run_probe(web, (200, b"")), ("up", 10, None, None))
        self.assertEqual(self.run_probe(web, (200, b""), ms=9000)[0], "degraded")
        self.assertEqual(self.run_probe(web, (503, b""))[:2], ("down", 10))

    def test_network_error_is_down_without_time(self):
        self.assertEqual(s.probe(CFG, CFG["components"][0], fetcher({"https://hub.example/": OSError("x")})), ("down", None, None, None))

    def test_a_component_can_set_its_own_slowness_limit(self):
        f = CFG["components"][2]
        self.assertEqual(self.run_probe(f, (206, b"x"), ms=2000)[0], "up")
        self.assertEqual(self.run_probe(f, (206, b"x"), ms=4000)[0], "degraded")

    def test_expected_status_list(self):
        f = CFG["components"][2]
        self.assertEqual(self.run_probe(f, (206, b"x"))[0], "up")
        self.assertEqual(self.run_probe(f, (302, b"x"))[0], "down")

    def test_expect_json(self):
        api = CFG["components"][1]
        self.assertEqual(self.run_probe(api, (200, b'{"api":"cheesecave","v":1}'))[0], "up")
        for bad in (b'{"api":"other"}', b"<html>", b"[1]"):
            self.assertEqual(self.run_probe(api, (200, bad))[0], "down")

    def test_expect_text(self):
        comp = {"name": "g", "path": "/g", "expect_text": "git-upload-pack"}
        cfg = {**CFG, "components": [comp]}
        for body, state in ((b"001e# service=git-upload-pack", "up"), (b"<html>", "down")):
            ticks = iter([0.0, 0.01])
            self.assertEqual(s.probe(cfg, comp, fetcher({"https://hub.example/g": (200, body)}), clock=lambda: next(ticks))[0], state)

    def test_speed_and_ttfb_are_measured_from_the_fetch_timings(self):
        f = CFG["components"][2]
        state, ms, ttfb, speed = self.run_probe(f, (206, b"x" * 2_000_000, 0.25, 0.5), ms=900)
        self.assertEqual((state, ms, ttfb, speed), ("up", 900, 250, 4.0))   # 2 MB in 0.5 s
        self.assertEqual(self.run_probe(f, (206, b"x" * 2_000_000, 0.25, 4.0), ms=900)[0], "degraded")  # 0.5 MB/s < min_speed 1.0

    def test_no_speed_unless_asked_for(self):
        self.assertIsNone(self.run_probe(CFG["components"][0], (200, b"x" * 1000, 0.1, 0.1))[3])

    def test_post_with_a_body_is_passed_on(self):
        seen = {}

        def fetch(url, headers, timeout, max_bytes, method, data):
            seen.update(method=method, data=data)
            return 200, b"{}"
        comp = {"name": "p", "path": "/p", "method": "POST", "body": "{\"a\":1}"}
        s.probe({**CFG, "components": [comp]}, comp, fetch)
        self.assertEqual(seen, {"method": "POST", "data": b'{"a":1}'})

    def test_check_all_names_every_component(self):
        table = {"https://hub.example/": (200, b""), "https://hub.example/api/version": (200, b'{"api":"cheesecave"}'),
                 "https://files.example/x": OSError("x"), "https://hub.example/u": (200, b"")}
        rec = s.check_all(CFG, NOW, fetcher(table))
        self.assertEqual(rec["t"], "2026-11-02T12:00:00Z")
        self.assertEqual([rec["c"][n][0] for n in ("Website", "API", "File", "Ungrouped")], ["up", "up", "down", "up"])


class Records(unittest.TestCase):
    def test_append_read_prune_and_torn_line(self):
        d = Path(tempfile.mkdtemp())
        for day in ("2026-10-20", "2026-11-01", "2026-11-02"):
            s.append_record(d, {"t": f"{day}T10:00:00Z", "c": {"A": ["up", 1]}})
        with (d / "checks" / "2026-11-02.jsonl").open("a") as f:
            f.write('{"t": "2026-11-02T11')  # torn write
        self.assertEqual([r["t"][:10] for r in s.read_records(d, NOW, 3)], ["2026-11-01", "2026-11-02"])
        s.prune(d, NOW, 3)
        self.assertEqual(sorted(p.stem for p in (d / "checks").glob("*.jsonl")), ["2026-11-01", "2026-11-02"])


class States(unittest.TestCase):
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

    def test_apply_actions_opens_closes_and_notifies(self):
        gh, inc, told = FakeGh(), [], []
        s.apply_actions(inc, [("open", "A")], NOW, gh, told.append)
        self.assertEqual((inc[0]["issue"], inc[0]["closed_at"], gh.opened), (1, None, ["A is down"]))
        s.apply_actions(inc, [("close", "A")], NOW, gh, told.append)
        self.assertEqual((inc[0]["closed_at"], gh.closed), ("2026-11-02T12:00:00Z", [1]))
        self.assertEqual(len(told), 2)
        self.assertIn("A is down", told[0])
        self.assertIn("reachable again", told[1])

    def test_github_or_the_webhook_refusing_does_not_lose_the_incident(self):
        def boom(text):
            raise RuntimeError("webhook down")
        gh, inc = FakeGh(fail=True), []
        s.apply_actions(inc, [("open", "A")], NOW, gh, boom)
        self.assertIsNone(inc[0]["issue"])
        s.apply_actions(inc, [("close", "A")], NOW, gh, boom)
        self.assertEqual(inc[0]["closed_at"], "2026-11-02T12:00:00Z")

    def test_save_incidents_drops_old_closed_ones(self):
        d = Path(tempfile.mkdtemp())
        old = {"component": "A", "title": "t", "url": None, "issue": None, "opened_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-02T00:00:00Z"}
        live = {**old, "closed_at": None}
        self.assertEqual(s.save_incidents(d, [old, live], NOW, 3), [live])
        self.assertEqual(s.load_incidents(d), [live])
        self.assertEqual(s.load_incidents(Path(tempfile.mkdtemp())), [])


class Notifier(unittest.TestCase):
    def test_none_without_a_url_and_the_shape_of_each_style(self):
        self.assertIsNone(s.make_notifier(url=""))
        sent = []
        for style, expected in (("slack", {"text": "hi"}), ("discord", {"content": "hi"}),
                                ("lark", {"msg_type": "text", "content": {"text": "hi"}})):
            s.make_notifier("https://hook", style, post=lambda u, p: sent.append((u, p)))("hi")
            self.assertEqual(sent[-1], ("https://hook", expected))


class Status(unittest.TestCase):
    OK = {"https://hub.example/": (200, b""), "https://hub.example/api/version": (200, b'{"api":"cheesecave"}'),
          "https://files.example/x": (206, b"x" * 1_000_000, 0.1, 0.5), "https://hub.example/u": (200, b"")}

    def test_tick_writes_status_views_and_hourly_rows(self):
        d = Path(tempfile.mkdtemp())
        st = s.tick(CFG, d, NOW, FakeGh(), fetcher(self.OK))
        self.assertEqual(sorted(p.name for p in (d / "view").glob("*.json")), sorted(f"{r}.json" for r in rollup.RANGES))
        self.assertEqual([p.name for p in (d / "hourly").glob("*.json")], ["2026-11.json"])
        by = {c["name"]: c for c in st["components"]}
        self.assertEqual((by["File"]["state"], by["File"]["speed"], by["File"]["ttfb_ms"], by["File"]["has_speed"]), ("up", 2.0, 100, True))
        self.assertEqual((by["Website"]["group"], by["Ungrouped"]["group"], by["Website"]["has_speed"]), ("Web", "Services", False))
        self.assertEqual(by["Website"]["uptime"]["24h"], 100.0)
        self.assertEqual(st["ranges"], list(rollup.RANGES))
        self.assertEqual(st["instance"], {"name": "Hub", "home_url": "https://hub.example"})
        self.assertEqual(json.loads((d / "status.json").read_text()), st)
        view = json.loads((d / "view" / "24h.json").read_text())
        self.assertEqual(view["components"]["Website"][-1][:3], [1, 0, 0])

    def test_description_is_passed_through(self):
        cfg = {**CFG, "description": "Our hub"}
        st = s.tick(cfg, tempfile.mkdtemp(), NOW, FakeGh(), fetcher(self.OK))
        self.assertEqual(st["instance"]["description"], "Our hub")

    def test_outage_opens_after_two_rounds_and_closes_on_recovery(self):
        d, gh, told = Path(tempfile.mkdtemp()), FakeGh(), []
        down = {**self.OK, "https://hub.example/": (500, b"")}
        s.tick(CFG, d, NOW, gh, fetcher(self.OK), told.append)
        st = s.tick(CFG, d, NOW + timedelta(minutes=5), gh, fetcher(down), told.append)
        self.assertEqual((st["components"][0]["state"], gh.opened), ("degraded", []))
        st = s.tick(CFG, d, NOW + timedelta(minutes=10), gh, fetcher(down), told.append)
        self.assertEqual((st["components"][0]["state"], gh.opened, len(told)), ("down", ["Website is down"], 1))
        st = s.tick(CFG, d, NOW + timedelta(minutes=15), gh, fetcher(self.OK), told.append)
        self.assertEqual((st["components"][0]["state"], gh.closed, len(told)), ("up", [1], 2))
        self.assertEqual(st["incidents"][0]["closed_at"], "2026-11-02T12:15:00Z")
        self.assertEqual(st["components"][0]["uptime"]["24h"], 50.0)   # 2 of 4 rounds failed

    def test_a_restart_keeps_the_hourly_history(self):
        d = Path(tempfile.mkdtemp())
        s.tick(CFG, d, NOW - timedelta(days=40), FakeGh(), fetcher(self.OK))
        for f in (d / "checks").glob("*.jsonl"):   # raw checks of that day are long gone
            f.unlink()
        st = s.tick(CFG, d, NOW, FakeGh(), fetcher(self.OK))
        self.assertEqual({c["name"]: c["uptime"]["90d"] for c in st["components"]}["Website"], 100.0)
        self.assertEqual({c["name"]: c["uptime"]["1y"] for c in st["components"]}["Website"], 100.0)


class PagesConfig(unittest.TestCase):
    def test_points_the_page_at_the_status_branch_of_the_repository(self):
        c = s.pages_config(CFG, "acme/status")
        self.assertEqual(c["data_base"], "https://raw.githubusercontent.com/acme/status/status/")
        self.assertEqual((c["name"], c["home_url"], c["fallback_base"]), ("Hub", "https://hub.example", ""))


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
