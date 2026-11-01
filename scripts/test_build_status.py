"""python3 -I scripts/test_build_status.py"""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_status as b  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class SiteNames(unittest.TestCase):
    def test_reads_names_in_order_and_stops_at_next_section(self):
        text = "owner: x\nsites:\n  - name: Website\n    url: u\n  - name: \"File download\"\n    url: v\nstatus-website:\n  name: Not a site\n"
        self.assertEqual(b.site_names(text), ["Website", "File download"])


class ParseChecks(unittest.TestCase):
    def test_understands_the_three_marks_and_ignores_other_commits(self):
        log = "\n".join([
            "2026-11-01T13:12:54+00:00|🟩 Website is up (200 in 211 ms) [skip ci] [upptime]",
            "2026-11-01T13:13:54+00:00|🟥 API is down (500 in 10 ms) [skip ci] [upptime]",
            "2026-11-01T13:14:54+00:00|🟨 File download performance is degraded (206 in 9000 ms) [skip ci] [upptime]",
            "2026-11-01T13:15:54+00:00|:bento: Update graphs [skip ci]",
        ])
        got = [(n, s) for _, n, s in b.parse_checks(log)]
        self.assertEqual(got, [("Website", "up"), ("API", "down"), ("File download", "degraded")])

    def test_sorts_oldest_first_and_converts_to_utc(self):
        log = "2026-11-01T21:00:00+08:00|🟩 A is up (200 in 1 ms)\n2026-11-01T12:00:00+00:00|🟩 A is up (200 in 1 ms)"
        whens = [w for w, _, _ in b.parse_checks(log)]
        self.assertEqual(whens[0].hour, 12)
        self.assertEqual(whens[1].hour, 13)


class DayColor(unittest.TestCase):
    def test_rules(self):
        z = {"up": 0, "down": 0, "degraded": 0}
        self.assertEqual(b.day_color(z), "none")
        self.assertEqual(b.day_color({**z, "up": 100}), "green")
        self.assertEqual(b.day_color({**z, "up": 99, "down": 1}), "yellow")
        self.assertEqual(b.day_color({**z, "up": 99, "degraded": 1}), "yellow")
        self.assertEqual(b.day_color({**z, "up": 19, "down": 1}), "red")


class Build(unittest.TestCase):
    def checks(self, rows):
        return [(datetime.fromisoformat(w).replace(tzinfo=timezone.utc), n, s) for w, n, s in rows]

    def test_bars_cover_the_window_and_missing_days_are_none(self):
        data = b.build(["A"], self.checks([("2026-11-01T10:00:00", "A", "up")]), [], NOW, days=3)
        bars = data["components"][0]["bars"]
        self.assertEqual([x["date"] for x in bars], ["2026-10-30", "2026-10-31", "2026-11-01"])
        self.assertEqual([x["color"] for x in bars], ["none", "none", "green"])
        self.assertIsNone(bars[0]["uptime"])

    def test_uptime_state_and_checked_at(self):
        rows = [("2026-11-01T09:00:00", "A", "up"), ("2026-11-01T10:00:00", "A", "down"),
                ("2026-11-01T11:00:00", "A", "up"), ("2026-11-01T11:00:00", "Other", "down")]
        c = b.build(["A"], self.checks(rows), [], NOW, days=2)["components"][0]
        self.assertEqual(c["state"], "up")
        self.assertEqual(c["checked_at"], "2026-11-01T11:00:00Z")
        self.assertEqual(c["uptime"], 66.67)

    def test_a_site_without_checks_is_unknown(self):
        c = b.build(["A"], [], [], NOW, days=2)["components"][0]
        self.assertEqual((c["state"], c["checked_at"], c["uptime"]), ("unknown", None, None))

    def test_incidents_are_passed_through(self):
        inc = [{"title": "t"}]
        self.assertEqual(b.build([], [], inc, NOW, days=1)["incidents"], inc)


if __name__ == "__main__":
    unittest.main()
