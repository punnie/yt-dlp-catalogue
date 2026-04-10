#!/usr/bin/env python3
"""Standalone test for the stagger scheduling logic."""

import sqlite3
from datetime import datetime, timezone, timedelta


def _get_scheduled_collections(db, now=None):
    """Copy of the scheduling logic from yt_archive.py."""
    now = now or datetime.now(timezone.utc)
    day_number = int(now.timestamp() / 86400)
    rows = db.execute("SELECT * FROM collections WHERE enabled = 1").fetchall()
    due = []
    for row in rows:
        interval = row["sync_interval_days"]
        last = row["last_synced_at"]

        if last is None:
            due.append(row)
            continue

        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        elapsed_days = (now - last_dt).total_seconds() / 86400

        if elapsed_days < 1:
            continue

        if elapsed_days >= interval:
            due.append(row)
            continue

        if day_number % interval == row["id"] % interval:
            due.append(row)

    return due


def mark_synced(db, collection_id, now):
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute(
        "UPDATE collections SET last_synced_at = ? WHERE id = ?",
        (ts, collection_id),
    )
    db.commit()


def setup_db(collections):
    """Create an in-memory DB with the given collections.

    collections: list of (name, interval) tuples.
    """
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            sync_interval_days INTEGER NOT NULL DEFAULT 1,
            last_synced_at TEXT
        );
    """)
    for name, interval in collections:
        db.execute(
            "INSERT INTO collections (name, url, sync_interval_days) VALUES (?, ?, ?)",
            (name, f"https://youtube.com/c/{name}", interval),
        )
    db.commit()
    return db


def test_daily_always_syncs():
    """Interval-1 collections sync every single day."""
    db = setup_db([("daily-A", 1), ("daily-B", 1)])
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

    for day in range(7):
        now = base + timedelta(days=day)
        due = _get_scheduled_collections(db, now)
        due_names = {r["name"] for r in due}
        assert due_names == {"daily-A", "daily-B"}, f"Day {day}: expected both, got {due_names}"
        for r in due:
            mark_synced(db, r["id"], now)

    print("PASS: test_daily_always_syncs")


def test_interval2_stagger():
    """Interval-2 collections split across even/odd days."""
    db = setup_db([(f"bi-{i}", 2) for i in range(6)])
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

    # Initial run: all are due (never synced)
    due = _get_scheduled_collections(db, base)
    for r in due:
        mark_synced(db, r["id"], base)

    day1_names = set()
    day2_names = set()

    for day in range(1, 15):
        now = base + timedelta(days=day)
        due = _get_scheduled_collections(db, now)
        due_names = {r["name"] for r in due}

        if day == 1:
            day1_names = due_names
        elif day == 2:
            day2_names = due_names

        for r in due:
            mark_synced(db, r["id"], now)

    # The two groups should partition all 6 collections
    assert day1_names | day2_names == {f"bi-{i}" for i in range(6)}, \
        f"Not all collections covered: {day1_names | day2_names}"
    assert day1_names & day2_names == set(), \
        f"Groups overlap: {day1_names & day2_names}"
    assert len(day1_names) > 0 and len(day2_names) > 0, \
        "One group is empty — no staggering"

    print(f"PASS: test_interval2_stagger — group A: {sorted(day1_names)}, group B: {sorted(day2_names)}")


def test_interval3_stagger():
    """Interval-3 collections split across 3 slots."""
    db = setup_db([(f"tri-{i}", 3) for i in range(9)])
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

    # Initial sync
    due = _get_scheduled_collections(db, base)
    for r in due:
        mark_synced(db, r["id"], base)

    groups = {}
    for day in range(1, 4):
        now = base + timedelta(days=day)
        due = _get_scheduled_collections(db, now)
        groups[day] = {r["name"] for r in due}
        for r in due:
            mark_synced(db, r["id"], now)

    all_names = {f"tri-{i}" for i in range(9)}
    covered = groups[1] | groups[2] | groups[3]
    assert covered == all_names, f"Not all covered: missing {all_names - covered}"

    # No collection should appear in two different slots
    for a in range(1, 4):
        for b in range(a + 1, 4):
            overlap = groups[a] & groups[b]
            assert overlap == set(), f"Day {a} and {b} overlap: {overlap}"

    print(f"PASS: test_interval3_stagger — slots: { {d: sorted(n) for d, n in groups.items()} }")


def test_no_double_sync():
    """Running --scheduled twice on the same day doesn't re-sync."""
    db = setup_db([("chan", 1)])
    now = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

    due = _get_scheduled_collections(db, now)
    assert len(due) == 1
    mark_synced(db, due[0]["id"], now)

    # Same day, a few hours later
    due = _get_scheduled_collections(db, now + timedelta(hours=6))
    assert len(due) == 0, f"Should not re-sync same day, got {[r['name'] for r in due]}"

    print("PASS: test_no_double_sync")


def test_catchup_after_downtime():
    """Overdue collections sync regardless of slot."""
    db = setup_db([("weekly", 7)])
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

    # Sync on day 0
    due = _get_scheduled_collections(db, base)
    mark_synced(db, due[0]["id"], base)

    # Cron was down, we resume 10 days later (overdue by 3 days)
    now = base + timedelta(days=10)
    due = _get_scheduled_collections(db, now)
    assert len(due) == 1, f"Expected catch-up sync, got {len(due)}"

    print("PASS: test_catchup_after_downtime")


def test_mixed_intervals():
    """Simulate 14 days with a realistic mix of intervals."""
    db = setup_db([
        ("news-1", 1), ("news-2", 1),           # daily
        ("tech-1", 2), ("tech-2", 2),            # every 2 days
        ("music-1", 7), ("music-2", 7),          # weekly
    ])
    base = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

    sync_counts = {name: 0 for name, _ in [
        ("news-1", 1), ("news-2", 1),
        ("tech-1", 2), ("tech-2", 2),
        ("music-1", 7), ("music-2", 7),
    ]}

    for day in range(14):
        now = base + timedelta(days=day)
        due = _get_scheduled_collections(db, now)
        for r in due:
            sync_counts[r["name"]] += 1
            mark_synced(db, r["id"], now)

    print(f"  Sync counts over 14 days: {sync_counts}")

    # Daily collections should sync 14 times
    assert sync_counts["news-1"] == 14
    assert sync_counts["news-2"] == 14
    # Bi-daily: ~7 each (the initial never-synced run on day 0 can cause
    # one extra for whichever slot doesn't align with day 0)
    assert sync_counts["tech-1"] + sync_counts["tech-2"] == 15
    assert abs(sync_counts["tech-1"] - sync_counts["tech-2"]) <= 1
    # Weekly: 2-3 each depending on slot alignment
    assert 2 <= sync_counts["music-1"] <= 3
    assert 2 <= sync_counts["music-2"] <= 3

    print("PASS: test_mixed_intervals")


if __name__ == "__main__":
    test_daily_always_syncs()
    test_interval2_stagger()
    test_interval3_stagger()
    test_no_double_sync()
    test_catchup_after_downtime()
    test_mixed_intervals()
    print("\nAll tests passed.")
