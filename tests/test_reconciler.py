from datetime import datetime, timedelta, timezone

from kiosk_sync.reconciler import (
    PunchEvent,
    SiteConfig,
    build_payroll_export_batch,
    deduplicate_punches,
    reconcile_shifts,
)

UTC = timezone.utc


def _dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 24, hour, minute, second, tzinfo=UTC)


def test_dedup_collapses_offline_double_tap_and_duplicate_id():
    events = [
        PunchEvent("e2", "emp-1", "k-01", "auckland-hq", "in", _dt(8, 0, 25), _dt(8, 10, 0), "biometric"),
        PunchEvent("e1", "emp-1", "k-01", "auckland-hq", "in", _dt(8, 0, 0), _dt(8, 10, 0), "biometric"),
        PunchEvent("e1", "emp-1", "k-01", "auckland-hq", "in", _dt(8, 0, 0), _dt(8, 10, 1), "biometric"),
        PunchEvent("e3", "emp-1", "k-01", "auckland-hq", "out", _dt(17, 0, 0), _dt(17, 0, 5), "biometric"),
    ]
    kept, dropped = deduplicate_punches(events, bounce_window_s=90)
    assert [e.event_id for e in kept] == ["e1", "e3"]
    assert set(dropped) == {"e2", "e1"}


def test_reconcile_shift_with_meal_break_rounding_and_overtime():
    sites = {"s1": SiteConfig("s1", -36.737, 174.706, radius_m=150.0)}
    events = [
        PunchEvent("p1", "emp-7", "k-02", "s1", "in", _dt(7, 58), _dt(8, 0), "kiosk"),
        PunchEvent("p2", "emp-7", "k-02", "s1", "out", _dt(18, 2), _dt(18, 3), "kiosk"),
    ]
    lines, dropped = reconcile_shifts(events, sites)
    assert dropped == []
    assert len(lines) == 1
    line = lines[0]
    assert line.break_deducted_min == 30
    assert line.regular_hours == 8.0
    assert line.overtime_hours == 1.5
    assert line.anomalies == ()


def test_mobile_geofence_breach_blocks_payroll_export_and_idempotency_is_stable():
    sites = {"s1": SiteConfig("s1", -36.737, 174.706, radius_m=100.0)}
    good_in = PunchEvent("g1", "emp-ok", "mob", "s1", "in", _dt(9, 0), _dt(9, 0), "mobile", -36.7371, 174.7061)
    good_out = PunchEvent("g2", "emp-ok", "mob", "s1", "out", _dt(13, 0), _dt(13, 0), "mobile", -36.7370, 174.7060)
    bad_in = PunchEvent("b1", "emp-far", "mob", "s1", "in", _dt(9, 0), _dt(9, 0), "mobile", -36.8500, 174.7600)
    bad_out = PunchEvent("b2", "emp-far", "mob", "s1", "out", _dt(13, 0), _dt(13, 0), "mobile", -36.7370, 174.7060)

    lines, _ = reconcile_shifts([good_in, good_out, bad_in, bad_out], sites)
    batch1 = build_payroll_export_batch("2026-W47", "xero", lines)
    batch2 = build_payroll_export_batch("2026-W47", "xero", lines)

    assert batch1["idempotency_key"] == batch2["idempotency_key"]
    assert batch1["entries"] == [{"employee_id": "emp-ok", "site_id": "s1", "regular_hours": 4.0, "overtime_hours": 0.0}]
    assert len(batch1["blocked_review"]) == 1
    assert "emp-far:geofence_breach:" in batch1["blocked_review"][0]


def test_future_clock_skew_flagged():
    sites = {"s1": SiteConfig("s1", -36.737, 174.706, max_clock_drift_s=60)}
    now = _dt(9, 0)
    events = [
        PunchEvent("c1", "emp-9", "k-03", "s1", "in", now + timedelta(minutes=5), now, "kiosk"),
        PunchEvent("c2", "emp-9", "k-03", "s1", "out", _dt(12, 0), _dt(12, 0), "kiosk"),
    ]
    lines, _ = reconcile_shifts(events, sites)
    assert lines[0].anomalies == ("future_clock_skew:300s",)
