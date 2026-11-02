"""Time buckets of the checks: the data behind the 24h / 7d / 30d / 90d / 1y views of the page.

Raw checks (one per component per round) are kept for a couple of weeks; every hour of them is also
rolled up into one row per component, kept for good. A view of 24h or 7d is made from the raw checks,
a longer one from the hourly rows.

A row is [n, degraded, down, p50_ms, p95_ms, speed_MBps]: counts of checks and the latency of the
checks that got an answer. A bucket without checks is None.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

# range -> (seconds per bucket, number of buckets); about 90 buckets each so the bars look alike
RANGES = {"24h": (900, 96), "7d": (7200, 84), "30d": (21600, 120), "90d": (86400, 90), "1y": (604800, 52)}
FROM_RAW = ("24h", "7d")
WEEK_OFFSET = 4 * 86400  # epoch day 0 is a Thursday; buckets of a week start on Monday


def epoch(iso_text):
    return int(datetime.fromisoformat(iso_text.replace("Z", "+00:00")).timestamp())


def iso_from_epoch(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hour_key(iso_text):
    return iso_text[:13]  # 2026-11-02T04


def percentile(values, q):
    ordered = sorted(values)
    return ordered[max(0, -(-len(ordered) * q // 100) - 1)]  # nearest rank, ceil without floats


def sample_row(samples):
    """samples: [(state, ms, speed)] -> [n, degraded, down, p50, p95, speed] (None for no samples)."""
    if not samples:
        return None
    times = [ms for state, ms, _ in samples if state != "down" and ms is not None]
    speeds = [s for _, _, s in samples if s is not None]
    return [
        len(samples),
        sum(1 for s in samples if s[0] == "degraded"),
        sum(1 for s in samples if s[0] == "down"),
        int(median(times)) if times else None,
        percentile(times, 95) if times else None,
        round(mean(speeds), 2) if speeds else None,
    ]


def merge_rows(rows):
    """Combine rows (of hours) into one bucket: counts add up, p50 is the median of the p50s, p95 the largest."""
    rows = [r for r in rows if r]
    if not rows:
        return None
    p50 = [r[3] for r in rows if r[3] is not None]
    p95 = [r[4] for r in rows if r[4] is not None]
    spd = [r[5] for r in rows if r[5] is not None]
    return [
        sum(r[0] for r in rows), sum(r[1] for r in rows), sum(r[2] for r in rows),
        int(median(p50)) if p50 else None, max(p95) if p95 else None,
        round(mean(spd), 2) if spd else None,
    ]


def record_sample(record, name):
    entry = record["c"].get(name)
    if entry is None:
        return None
    state, ms = entry[0], entry[1]
    speed = entry[3] if len(entry) > 3 else None
    return state, ms, speed


# ---------------------------------------------------------------- hourly rows on disk

def hourly_from_records(records, names):
    """{hour: {name: row}} for every hour that the raw records touch."""
    samples = {}
    for r in records:
        for name in names:
            s = record_sample(r, name)
            if s:
                samples.setdefault(hour_key(r["t"]), {}).setdefault(name, []).append(s)
    return {h: {n: sample_row(v) for n, v in by.items()} for h, by in samples.items()}


def load_hourly(data_dir):
    hourly = {}
    for path in sorted((Path(data_dir) / "hourly").glob("*.json")):
        try:
            hourly.update(json.loads(path.read_text(encoding="utf-8"))["hours"])
        except (ValueError, KeyError):
            pass
    return hourly


def save_hourly(data_dir, hourly, touched_hours):
    """Write the month files that contain a touched hour (older months stay as they are on disk)."""
    folder = Path(data_dir) / "hourly"
    folder.mkdir(parents=True, exist_ok=True)
    for month in sorted({h[:7] for h in touched_hours}):
        hours = {h: v for h, v in sorted(hourly.items()) if h[:7] == month}
        (folder / f"{month}.json").write_text(json.dumps({"hours": hours}, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- views

def view_window(rng, now):
    step, count = RANGES[rng]
    offset = WEEK_OFFSET if step == 604800 else 0
    last = (int(now.timestamp()) - offset) // step
    first = last - count + 1
    return step, count, offset, first, first * step + offset


def build_view(rng, names, now, records, hourly):
    step, count, offset, first, start = view_window(rng, now)
    cells = {n: [[] for _ in range(count)] for n in names}
    if rng in FROM_RAW:
        for r in records:
            i = (epoch(r["t"]) - offset) // step - first
            if 0 <= i < count:
                for n in names:
                    s = record_sample(r, n)
                    if s:
                        cells[n][i].append(s)
        rows = {n: [sample_row(c) for c in cells[n]] for n in names}
    else:
        for hour, by in hourly.items():
            i = (epoch(hour + ":00:00Z") - offset) // step - first
            if 0 <= i < count:
                for n in names:
                    if by.get(n):
                        cells[n][i].append(by[n])
        rows = {n: [merge_rows(c) for c in cells[n]] for n in names}
    return {"range": rng, "step_seconds": step, "start": iso_from_epoch(start), "count": count, "components": rows}


def uptime(rows):
    """Percent of checks that got an answer (slow ones count as up), None without checks."""
    n = sum(r[0] for r in rows if r)
    return round(100 * (n - sum(r[2] for r in rows if r)) / n, 2) if n else None
