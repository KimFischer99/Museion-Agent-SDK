"""P1 scheduler tests: interval anchoring and drift, local-calendar
occurrence resolution with DST gap/fold, monthly day handling, misfire
policies after downtime, clock rollback, edit/pause races, and the due
index scan (SPEC §5, §16.1 Scheduling row, SCHED-01)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from proactive_sdk import AdmissionReport, FakeClock, JobSpec, PASError, Store
from proactive_sdk.scheduler import (
    Scheduler,
    interval_slot_ms,
    latest_due_slot,
    next_occurrence_after,
    parse_schedule,
    resolve_local_wall,
)

T0 = 1_760_000_000_000  # 2025-10-09T08:53:20Z; arbitrary fixed wall time


def utc_ms(text: str) -> int:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1000)


def make_scheduler(path, clock):
    store = Store(str(path), profile="demo", owner_destination="local-inbox:demo", clock=clock)
    return store, Scheduler(store, clock)


class IntervalSlotTests(unittest.TestCase):
    def test_slot_is_anchored_and_stable(self):
        anchor = utc_ms("2026-01-01T00:00:00Z")
        every = 1800
        self.assertEqual(
            interval_slot_ms(anchor, every, anchor), anchor
        )
        self.assertEqual(
            interval_slot_ms(anchor, every, anchor + 1799_999), anchor
        )
        self.assertEqual(
            interval_slot_ms(anchor, every, anchor + 1800_000), anchor + 1800_000
        )

    def test_slot_before_anchor_is_virtual_not_negative_chaos(self):
        anchor = utc_ms("2026-01-01T00:00:00Z")
        slot = interval_slot_ms(anchor, 1800, anchor - 1)
        self.assertLess(slot, anchor)
        self.assertEqual((anchor - slot) % 1_800_000, 0)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            interval_slot_ms(0, 0, 1000)
        with self.assertRaises(TypeError):
            interval_slot_ms(0.5, 1800, 1000)


class LocalWallResolutionTests(unittest.TestCase):
    """DST rules on real zone data: Europe/Berlin springs forward on
    2026-03-29 (02:00->03:00) and falls back on 2026-10-25 (03:00->02:00)."""

    berlin = ZoneInfo("Europe/Berlin")

    def test_normal_time_resolves_to_single_instant(self):
        slots = resolve_local_wall(date(2026, 3, 28), 9, 0, self.berlin)
        self.assertEqual(slots, [utc_ms("2026-03-28T08:00:00Z")])  # CET = UTC+1

    def test_spring_gap_is_skipped(self):
        self.assertEqual(resolve_local_wall(date(2026, 3, 29), 2, 30, self.berlin), [])

    def test_fall_back_repetition_collapses_to_earliest_by_default(self):
        slots = resolve_local_wall(date(2026, 10, 25), 2, 30, self.berlin)
        self.assertEqual(slots, [utc_ms("2026-10-25T00:30:00Z")])  # first CEST pass

    def test_fall_back_latest_policy_picks_second_instant(self):
        slots = resolve_local_wall(date(2026, 10, 25), 2, 30, self.berlin, fold_policy="latest")
        self.assertEqual(slots, [utc_ms("2026-10-25T01:30:00Z")])  # second CET pass

    def test_daily_offset_shifts_across_transitions(self):
        sched = parse_schedule(
            {"kind": "daily", "local_time": "09:00", "timezone": "Europe/Berlin"}
        )
        self.assertEqual(
            latest_due_slot(sched, utc_ms("2026-03-29T10:00:00Z"), utc_ms("2026-03-28T00:00:00Z")),
            utc_ms("2026-03-29T07:00:00Z"),  # CEST = UTC+2
        )
        self.assertEqual(
            latest_due_slot(sched, utc_ms("2026-10-25T10:00:00Z"), utc_ms("2026-10-24T00:00:00Z")),
            utc_ms("2026-10-25T08:00:00Z"),  # back to CET = UTC+1
        )

    def test_gap_day_skips_nonexistent_slot_but_fires_next_day(self):
        sched = parse_schedule(
            {"kind": "daily", "local_time": "02:30", "timezone": "Europe/Berlin"}
        )
        cursor = utc_ms("2026-03-28T01:30:00Z")  # 2026-03-28 02:30 CET
        latest = latest_due_slot(sched, utc_ms("2026-03-29T12:00:00Z"), cursor)
        self.assertIsNone(latest)  # 03-29 02:30 does not exist -> skipped
        nxt = next_occurrence_after(sched, utc_ms("2026-03-29T12:00:00Z"))
        self.assertEqual(nxt, utc_ms("2026-03-30T00:30:00Z"))  # CEST = UTC+2


class CalendarScheduleTests(unittest.TestCase):
    def test_weekday_filter_is_iso_based(self):
        sched = parse_schedule(
            {"kind": "weekly", "weekdays": [1], "local_time": "09:00",
             "timezone": "Europe/Berlin"}
        )
        # 2026-10-05 is a Monday, 2026-10-06 a Tuesday.
        self.assertEqual(
            latest_due_slot(sched, utc_ms("2026-10-06T10:00:00Z"), utc_ms("2026-10-04T00:00:00Z")),
            utc_ms("2026-10-05T07:00:00Z"),
        )

    def test_monthly_31_skips_short_months_without_clamping(self):
        sched = parse_schedule(
            {"kind": "monthly", "day_of_month": 31, "local_time": "09:00",
             "timezone": "Europe/Berlin"}
        )
        nxt = next_occurrence_after(sched, utc_ms("2026-01-31T08:00:00Z"))
        # February skipped; March 31 is two days after the DST jump -> CEST.
        self.assertEqual(nxt, utc_ms("2026-03-31T07:00:00Z"))

    def test_monthly_29_handles_leap_years(self):
        sched = parse_schedule(
            {"kind": "monthly", "day_of_month": 29, "local_time": "09:00",
             "timezone": "Europe/Berlin"}
        )
        nxt = next_occurrence_after(sched, utc_ms("2026-01-29T08:00:00Z"))
        # No Feb 29 in 2026; March 29 is the DST transition day, so 09:00
        # local is already CEST (= UTC+2).
        self.assertEqual(nxt, utc_ms("2026-03-29T07:00:00Z"))
        nxt_leap = next_occurrence_after(sched, utc_ms("2028-01-29T08:00:00Z"))
        self.assertEqual(nxt_leap, utc_ms("2028-02-29T08:00:00Z"))  # 2028 is a leap year

    def test_runonce_occurrence_window(self):
        sched = parse_schedule({"kind": "runonce", "at": "2026-01-05T09:00:00Z"})
        self.assertIsNone(latest_due_slot(sched, utc_ms("2026-01-05T08:59:59Z"), 0))
        self.assertEqual(
            latest_due_slot(sched, utc_ms("2026-01-05T09:00:01Z"), 0),
            utc_ms("2026-01-05T09:00:00Z"),
        )
        self.assertIsNone(latest_due_slot(sched, utc_ms("2026-01-05T09:00:01Z"),
                                          utc_ms("2026-01-05T09:00:00Z")))


class MisfireTests(unittest.TestCase):
    """Three policies over the same interval job: at most one catch-up and
    a queryable reason for what did not run (SPEC §5.2). Anchor is
    2026-10-01T09:00:00Z; the job is registered at 08:55, so the first
    real slot is 09:00."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.db")
        self.clock = FakeClock(wall_ms=utc_ms("2026-10-01T08:55:00Z"))

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, *, mode, misfire=None, every_seconds=1800):
        store, scheduler = make_scheduler(self.db, self.clock)
        spec = JobSpec(
            job_id="job-1",
            mode=mode,
            schedule={
                "kind": "interval",
                "anchor": "2026-10-01T09:00:00Z",
                "every_seconds": every_seconds,
            },
            task={"instruction": "检查。"},
            misfire_policy=misfire,
        )
        scheduler.register_job(spec, idempotency_key="k1")
        return store, scheduler

    def test_heartbeat_coalesces_to_exactly_one_check(self):
        store, scheduler = self._register(mode="heartbeat")
        self.clock.set_wall(utc_ms("2026-10-01T09:00:30Z"))
        first = scheduler.admit_due()
        self.assertEqual(len(first.admitted), 1)
        self.clock.advance_wall(7 * 86_400_000)  # one week down
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)  # coalesced, not 336 runs
        self.assertEqual(report.expired, [])
        admitted_rows = [
            o for o in store.occurrences("job-1") if o["state"] == "admitted"
        ]
        self.assertEqual(len(admitted_rows), 2)
        store.close()

    def test_task_beyond_grace_expires_with_reason(self):
        store, scheduler = self._register(mode="task", misfire="grace_once")
        self.clock.set_wall(utc_ms("2026-10-01T09:00:30Z"))
        self.assertEqual(len(scheduler.admit_due().admitted), 1)
        # Land the tick > grace (900 s) after the newest missed slot.
        self.clock.advance_wall(7 * 86_400_000 + 1_200_000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertEqual(len(report.expired), 1)
        occurrence_id, reason = report.expired[0]
        self.assertIn("missed_beyond_grace", reason)
        self.assertIn("episode_slots=336", reason)  # one week of 30-min slots
        expired = [r for r in store.occurrences("job-1") if r["state"] == "expired"]
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["reason"], reason)
        self.assertEqual(expired[0]["occurrence_id"], occurrence_id)
        store.close()

    def test_task_within_grace_catches_up_once(self):
        store, scheduler = self._register(mode="task", misfire="grace_once")
        self.clock.set_wall(utc_ms("2026-10-01T09:00:30Z"))
        self.assertEqual(len(scheduler.admit_due().admitted), 1)
        self.clock.advance_wall(1800_000 + 90_000)  # one period, 90 s late
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)
        store.close()

    def test_strict_expire_policy_only_admits_healthy_ticks(self):
        store, scheduler = self._register(mode="heartbeat", misfire="expire")
        self.clock.set_wall(utc_ms("2026-10-01T09:00:30Z"))
        self.assertEqual(len(scheduler.admit_due().admitted), 1)
        # Land the tick beyond the healthy-tick tolerance (60 s).
        self.clock.advance_wall(7 * 86_400_000 + 120_000)
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertIn("missed_beyond_catchup_tolerance", report.expired[0][1])
        # After the episode, a tick that lands on the next slot admits again.
        next_due = store.get_job("job-1").next_due_ms
        self.clock.set_wall(next_due + 30_000)
        again = scheduler.admit_due()
        self.assertEqual(len(again.admitted), 1)
        store.close()

    def test_consecutive_expired_episodes_do_not_double_count(self):
        store, scheduler = self._register(mode="task", misfire="expire")
        self.clock.set_wall(utc_ms("2026-10-01T09:00:30Z"))
        self.assertEqual(len(scheduler.admit_due().admitted), 1)
        self.clock.set_wall(utc_ms("2026-10-01T12:05:00Z"))  # 5 min after slot 12:00
        first = scheduler.admit_due()
        self.assertIn("episode_slots=6", first.expired[0][1])  # 09:30..12:00
        self.clock.set_wall(utc_ms("2026-10-01T13:05:00Z"))
        second = scheduler.admit_due()
        self.assertIn("episode_slots=2", second.expired[0][1])  # 12:30,13:00 only
        store.close()

    def test_job_deadline_blocks_admission(self):
        store, scheduler = self._register(mode="task", misfire="grace_once")
        store.upsert_job(
            JobSpec(
                job_id="job-1",
                mode="task",
                schedule={
                    "kind": "interval",
                    "anchor": "2026-10-01T09:00:00Z",
                    "every_seconds": 1800,
                },
                task={"instruction": "检查。"},
                revision=2,
                deadline="2026-10-01T00:00:00Z",  # already in the past
            ),
            idempotency_key="k2",
        )
        self.clock.set_wall(utc_ms("2026-10-01T09:00:30Z"))
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])
        self.assertIn("past_job_deadline", report.expired[0][1])
        store.close()


class DowntimeAndRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_clock_rollback_does_not_replay_admitted_slots(self):
        clock = FakeClock(wall_ms=T0)
        store, scheduler = make_scheduler(self.db, clock)
        scheduler.register_job(
            JobSpec(
                job_id="hb-rb",
                mode="heartbeat",
                schedule={"kind": "interval",
                          "anchor": "2025-10-09T09:00:00Z", "every_seconds": 1800},
                task={"instruction": "检查。"},
            ),
            idempotency_key="k",
        )
        clock.advance_wall(1800_000)  # 09:23:20 -> admits the 09:00 slot
        self.assertEqual(len(scheduler.admit_due().admitted), 1)
        clock.advance_wall(1800_000)  # 09:53:20 -> admits the 09:30 slot
        self.assertEqual(len(scheduler.admit_due().admitted), 1)

        clock.set_wall(T0)  # operator rolls the wall clock back to 08:53
        report = scheduler.admit_due()
        self.assertEqual(report.admitted, [])

        clock.advance_wall(4_030_000)  # wall catches up to 10:00:30
        self.assertEqual(len(scheduler.admit_due().admitted), 1)  # slot 10:00, once
        self.assertEqual(len(store.occurrences("hb-rb")), 3)
        store.close()

    def test_daily_heartbeat_after_week_of_downtime_sends_once(self):
        clock = FakeClock(wall_ms=utc_ms("2026-10-01T06:00:00Z"))  # 08:00 CEST
        store, scheduler = make_scheduler(self.db, clock)
        scheduler.register_job(
            JobSpec(
                job_id="daily-agenda",
                mode="heartbeat",
                schedule={"kind": "daily", "local_time": "09:00",
                          "timezone": "Europe/Berlin"},
                task={"instruction": "总结日程。"},
            ),
            idempotency_key="k",
        )
        self.assertEqual(store.get_job("daily-agenda").next_due_ms,
                         utc_ms("2026-10-01T07:00:00Z"))  # today 09:00 CEST
        clock.advance_wall(3600_000)
        self.assertEqual(len(scheduler.admit_due().admitted), 1)
        # Machine off for a week; on return exactly one catch-up.
        clock.advance_wall(7 * 86_400_000)
        report = scheduler.admit_due()
        self.assertEqual(len(report.admitted), 1)
        self.assertEqual(
            len([o for o in store.occurrences("daily-agenda") if o["state"] == "admitted"]),
            2,
        )
        store.close()


class RegistrationAndRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_daily_job_created_before_local_time(self):
        clock = FakeClock(wall_ms=utc_ms("2026-10-06T08:00:00Z"))  # 10:00 CEST
        store, scheduler = make_scheduler(self.db, clock)
        scheduler.register_job(
            JobSpec(
                job_id="agenda",
                mode="task",
                schedule={"kind": "daily", "local_time": "09:00",
                          "timezone": "Europe/Berlin"},
                task={"instruction": "总结。"},
            ),
            idempotency_key="k",
        )
        record = store.get_job("agenda")
        # 10:00 local already past 09:00 -> first run tomorrow 09:00 CEST.
        self.assertEqual(record.next_due_ms, utc_ms("2026-10-07T07:00:00Z"))
        store.close()

    def test_edit_bumps_revision_and_resets_next_due(self):
        clock = FakeClock(wall_ms=T0)
        store, scheduler = make_scheduler(self.db, clock)
        scheduler.register_job(
            JobSpec(
                job_id="hb",
                mode="heartbeat",
                schedule={"kind": "interval",
                          "anchor": "2025-10-09T09:00:00Z", "every_seconds": 1800},
                task={"instruction": "检查。"},
            ),
            idempotency_key="k",
        )
        clock.advance_wall(1800_000)
        scheduler.admit_due()
        clock.advance_wall(600_000)
        edited = store.upsert_job(
            JobSpec(
                job_id="hb",
                mode="heartbeat",
                schedule={"kind": "interval",
                          "anchor": "2025-10-09T09:00:00Z", "every_seconds": 600},
                task={"instruction": "检查，改频率。"},
                revision=2,
            ),
            idempotency_key="k2",
        )
        self.assertEqual(edited.revision, 2)
        self.assertEqual(edited.next_due_ms, clock.wall_now_ms())
        store.close()

    def test_pause_wins_over_in_flight_admission(self):
        clock = FakeClock(wall_ms=T0)
        store, scheduler = make_scheduler(self.db, clock)
        scheduler.register_job(
            JobSpec(
                job_id="hb",
                mode="heartbeat",
                schedule={"kind": "interval",
                          "anchor": "2025-10-09T09:00:00Z", "every_seconds": 1800},
                task={"instruction": "检查。"},
            ),
            idempotency_key="k",
        )
        clock.advance_wall(1800_000)
        snapshot = store.get_job("hb")  # scheduler reads the job...
        store.set_job_enabled("hb", expected_revision=1, enabled=False)  # ...user pauses
        # Admission built for the stale snapshot must not enqueue anything.
        outcome = store.admit_job_occurrence(
            snapshot.job_id,
            expected_revision=snapshot.revision,
            kind="interval",
            slot_ms=snapshot.next_due_ms,
            next_due_ms=snapshot.next_due_ms + 1800_000,
            now_ms=clock.wall_now_ms(),
        )
        self.assertEqual(outcome, "skipped_revision")
        self.assertEqual(store.db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        report = scheduler.admit_due()  # and the loop sees the paused job no more
        self.assertEqual(report.evaluated, 0)
        store.close()

    def test_pause_freezes_and_resume_reevaluates(self):
        clock = FakeClock(wall_ms=T0)
        store, scheduler = make_scheduler(self.db, clock)
        scheduler.register_job(
            JobSpec(
                job_id="hb",
                mode="heartbeat",
                schedule={"kind": "interval",
                          "anchor": "2025-10-09T09:00:00Z", "every_seconds": 1800},
                task={"instruction": "检查。"},
            ),
            idempotency_key="k",
        )
        paused = store.set_job_enabled("hb", expected_revision=1, enabled=False)
        frozen_due = paused.next_due_ms
        clock.advance_wall(3600_000)
        self.assertEqual(scheduler.admit_due().evaluated, 0)  # paused: invisible
        resumed = store.set_job_enabled("hb", expected_revision=2, enabled=True)
        self.assertEqual(resumed.next_due_ms, clock.wall_now_ms())
        report = scheduler.admit_due()  # coalesce_latest: one catch-up, not two
        self.assertEqual(len(report.admitted), 1)
        self.assertIsNotNone(frozen_due)
        store.close()


class ScanEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "p.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_due_scan_uses_index_not_full_table(self):
        clock = FakeClock(wall_ms=T0)
        store, _ = make_scheduler(self.db, clock)
        plan = store.db.execute(
            """EXPLAIN QUERY PLAN SELECT job_id FROM jobs
               WHERE enabled=1 AND scheduler_owner='pas'
                 AND next_due_ms IS NOT NULL AND next_due_ms<=?""",
            (T0,),
        ).fetchall()
        detail = " ".join(str(row[-1]) for row in plan)
        self.assertIn("jobs_due", detail)
        store.close()

    def test_thousand_jobs_scan_is_bounded(self):
        import time

        clock = FakeClock(wall_ms=T0)
        store, scheduler = make_scheduler(self.db, clock)
        specs = []
        for i in range(1000):
            specs.append(
                JobSpec(
                    job_id=f"hb-{i:04d}",
                    mode="heartbeat",
                    schedule={
                        "kind": "interval",
                        "anchor": "2025-10-09T09:00:00Z",
                        "every_seconds": 1800 + i,  # staggered periods
                    },
                    task={"instruction": "轻量检查。"},
                )
            )
        start = time.monotonic()
        for i, spec in enumerate(specs):
            scheduler.register_job(spec, idempotency_key=f"k{i}")
        clock.advance_wall(3600_000)
        report = scheduler.admit_due()
        elapsed = time.monotonic() - start
        self.assertEqual(len(report.admitted), 1000)  # every job had a due slot by now
        self.assertLess(elapsed, 30.0)  # environment-dependent guard, not a benchmark claim
        store.close()

    def test_host_owned_jobs_are_never_admitted_by_pas(self):
        clock = FakeClock(wall_ms=T0)
        store, scheduler = make_scheduler(self.db, clock)
        store.upsert_job(
            JobSpec(
                job_id="host-task",
                mode="task",
                owner="host",
                schedule={"kind": "interval",
                          "anchor": "2025-10-09T09:00:00Z", "every_seconds": 1800},
                task={"instruction": "宿主负责调度。"},
            ),
            idempotency_key="k",
        )
        clock.advance_wall(3600_000)
        report = scheduler.admit_due()
        self.assertEqual(report.evaluated, 0)
        store.close()


if __name__ == "__main__":
    unittest.main()
