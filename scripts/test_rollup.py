"""python3 -I scripts/test_rollup.py"""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rollup as r  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 7, tzinfo=timezone.utc)


def rec(t, **comps):
    return {"t": t, "c": {k: list(v) for k, v in comps.items()}}


class Rows(unittest.TestCase):
    def test_percentile_nearest_rank(self):
        self.assertEqual(r.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95), 10)
        self.assertEqual(r.percentile([5], 95), 5)
        self.assertEqual(r.percentile(list(range(1, 101)), 95), 95)

    def test_sample_row(self):
        self.assertIsNone(r.sample_row([]))
        row = r.sample_row([("up", 100, None), ("down", None, None), ("degraded", 900, 2.5), ("up", 300, 3.5)])
        self.assertEqual(row, [4, 1, 1, 300, 900, 3.0])
        self.assertEqual(r.sample_row([("down", None, None)]), [1, 0, 1, None, None, None])

    def test_merge_rows(self):
        self.assertIsNone(r.merge_rows([None, None]))
        a, b = [10, 1, 0, 100, 300, 2.0], [10, 0, 2, 200, 900, None]
        self.assertEqual(r.merge_rows([a, None, b]), [20, 1, 2, 150, 900, 2.0])
        self.assertEqual(r.merge_rows([[1, 0, 1, None, None, None]]), [1, 0, 1, None, None, None])

    def test_record_sample_tolerates_short_entries(self):
        self.assertEqual(r.record_sample(rec("t", A=["up", 5]), "A"), ("up", 5, None))
        self.assertEqual(r.record_sample(rec("t", A=["up", 5, 3, 2.5]), "A"), ("up", 5, 2.5))
        self.assertIsNone(r.record_sample(rec("t", A=["up", 5]), "B"))

    def test_uptime(self):
        self.assertIsNone(r.uptime([None]))
        self.assertEqual(r.uptime([[10, 5, 1, 1, 1, None], None, [10, 0, 0, 1, 1, None]]), 95.0)


class Hourly(unittest.TestCase):
    def test_hourly_from_records_and_disk_roundtrip(self):
        records = [rec("2026-11-02T04:00:10Z", A=("up", 10, 1, None)), rec("2026-11-02T04:30:10Z", A=("down", None, None, None)),
                   rec("2026-10-31T23:55:00Z", A=("up", 30, 1, None))]
        hourly = r.hourly_from_records(records, ["A", "B"])
        self.assertEqual(hourly["2026-11-02T04"]["A"], [2, 0, 1, 10, 10, None])
        self.assertNotIn("B", hourly["2026-11-02T04"])
        d = Path(tempfile.mkdtemp())
        r.save_hourly(d, hourly, hourly)
        self.assertEqual(sorted(p.name for p in (d / "hourly").glob("*.json")), ["2026-10.json", "2026-11.json"])
        self.assertEqual(r.load_hourly(d), hourly)

    def test_save_only_rewrites_touched_months_and_bad_files_are_skipped(self):
        d = Path(tempfile.mkdtemp())
        old = {"2026-09-01T00": {"A": [1, 0, 0, 5, 5, None]}}
        r.save_hourly(d, old, old)
        (d / "hourly" / "2026-08.json").write_text("not json")
        loaded = r.load_hourly(d)
        self.assertEqual(loaded, old)
        before = (d / "hourly" / "2026-09.json").read_text()
        new = {**old, "2026-11-02T04": {"A": [1, 0, 0, 7, 7, None]}}
        r.save_hourly(d, new, {"2026-11-02T04": 1})
        self.assertEqual((d / "hourly" / "2026-09.json").read_text(), before)
        self.assertTrue((d / "hourly" / "2026-11.json").exists())


class Views(unittest.TestCase):
    def test_window_of_every_range_ends_in_the_current_bucket(self):
        for rng, (step, count) in r.RANGES.items():
            _, _, offset, first, start = r.view_window(rng, NOW)
            self.assertLessEqual(start + (count - 1) * step, int(NOW.timestamp()) - 0)
            self.assertGreater(start + count * step, int(NOW.timestamp()))
        self.assertEqual(datetime.fromisoformat(r.build_view("1y", ["A"], NOW, [], {})["start"].replace("Z", "+00:00")).weekday(), 0)

    def test_24h_view_from_raw(self):
        records = [rec("2026-11-02T12:01:00Z", A=("up", 100, 1, None)), rec("2026-11-02T12:06:00Z", A=("down", None, None, None)),
                   rec("2026-11-02T10:00:00Z", A=("up", 50, 1, None)), rec("2026-10-01T10:00:00Z", A=("up", 1, 1, None))]
        view = r.build_view("24h", ["A"], NOW, records, {})
        rows = view["components"]["A"]
        self.assertEqual(len(rows), 96)
        self.assertEqual(rows[-1], [2, 0, 1, 100, 100, None])   # 12:00-12:15
        self.assertEqual(rows[-9], [1, 0, 0, 50, 50, None])     # 10:00-10:15
        self.assertEqual(sum(1 for x in rows if x), 2)            # the record of October is outside
        self.assertEqual((view["range"], view["step_seconds"], view["count"]), ("24h", 900, 96))

    def test_90d_view_from_hourly_rows_merges_hours_of_a_day(self):
        hourly = {"2026-11-02T04": {"A": [12, 0, 0, 100, 200, None]}, "2026-11-02T05": {"A": [12, 1, 0, 300, 900, None]},
                  "2026-11-01T05": {"A": [12, 0, 12, None, None, None]}, "2025-01-01T00": {"A": [1, 0, 0, 1, 1, None]}}
        rows = r.build_view("90d", ["A", "B"], NOW, [], hourly)["components"]
        self.assertEqual(rows["A"][-1], [24, 1, 0, 200, 900, None])
        self.assertEqual(rows["A"][-2], [12, 0, 12, None, None, None])
        self.assertEqual(sum(1 for x in rows["A"] if x), 2)       # the hour of 2025 is outside
        self.assertTrue(all(x is None for x in rows["B"]))


if __name__ == "__main__":
    unittest.main()
