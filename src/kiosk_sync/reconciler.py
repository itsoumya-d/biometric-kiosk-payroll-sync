"""Offline kiosk punch deduplication, geofence/clock-drift checks, and payroll batch export."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Iterable


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    lat: float
    lon: float
    radius_m: float = 150.0
    max_clock_drift_s: int = 120


@dataclass(frozen=True)
class PunchEvent:
    event_id: str
    employee_id: str
    terminal_id: str
    site_id: str
    kind: str  # "in" | "out"
    recorded_at: datetime
    received_at: datetime
    source: str  # "biometric" | "kiosk" | "mobile"
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class PayrollLine:
    employee_id: str
    site_id: str
    regular_hours: float
    overtime_hours: float
    break_deducted_min: int
    anomalies: tuple[str, ...] = field(default_factory=tuple)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _round_hours(minutes: float, increment_min: int) -> float:
    if increment_min <= 1:
        return round(minutes / 60.0, 2)
    rounded = round(minutes / increment_min) * increment_min
    return round(rounded / 60.0, 2)


def deduplicate_punches(
    events: Iterable[PunchEvent],
    bounce_window_s: int = 90,
) -> tuple[list[PunchEvent], list[str]]:
    """Sort offline replayed punches and collapse rapid double-tap bounce events."""
    ordered = sorted(events, key=lambda e: (e.employee_id, e.recorded_at, e.event_id))
    kept: list[PunchEvent] = []
    dropped_ids: list[str] = []
    seen_ids: set[str] = set()

    for ev in ordered:
        if ev.event_id in seen_ids:
            dropped_ids.append(ev.event_id)
            continue
        seen_ids.add(ev.event_id)
        if kept and kept[-1].employee_id == ev.employee_id and kept[-1].kind == ev.kind:
            delta = (ev.recorded_at - kept[-1].recorded_at).total_seconds()
            if 0 <= delta <= bounce_window_s:
                dropped_ids.append(ev.event_id)
                continue
        kept.append(ev)

    return kept, dropped_ids


def validate_punch_integrity(ev: PunchEvent, site: SiteConfig) -> list[str]:
    """Flag mobile geofence violations and offline kiosk NTP clock drift anomalies."""
    flags: list[str] = []
    if ev.source == "mobile":
        if ev.lat is None or ev.lon is None:
            flags.append("missing_gps_coordinates")
        else:
            dist = _haversine_m(site.lat, site.lon, ev.lat, ev.lon)
            if dist > site.radius_m:
                flags.append(f"geofence_breach:{int(round(dist))}m")
    # Offline replays arrive strictly later; negative drift indicates future-skewed kiosk clock.
    skew = (ev.recorded_at - ev.received_at).total_seconds()
    if skew > site.max_clock_drift_s:
        flags.append(f"future_clock_skew:{int(round(skew))}s")
    return flags


def reconcile_shifts(
    events: Iterable[PunchEvent],
    sites: dict[str, SiteConfig],
    ordinary_daily_hours: float = 8.0,
    unpaid_break_threshold_h: float = 5.0,
    unpaid_break_min: int = 30,
    rounding_increment_min: int = 15,
) -> tuple[list[PayrollLine], list[str]]:
    """Pair validated clock-in/out punches into rounded payroll lines with break rules."""
    cleaned, dropped = deduplicate_punches(events)
    active_in: dict[str, PunchEvent] = {}
    lines: list[PayrollLine] = []

    for ev in cleaned:
        site = sites.get(ev.site_id)
        flags = validate_punch_integrity(ev, site) if site else [f"unknown_site:{ev.site_id}"]

        if ev.kind == "in":
            active_in[ev.employee_id] = ev
            continue

        start = active_in.pop(ev.employee_id, None)
        if start is None or ev.recorded_at <= start.recorded_at:
            dropped.append(ev.event_id)
            continue

        raw_min = (ev.recorded_at - start.recorded_at).total_seconds() / 60.0
        break_min = unpaid_break_min if (raw_min / 60.0) > unpaid_break_threshold_h else 0
        net_hours = _round_hours(max(raw_min - break_min, 0.0), rounding_increment_min)
        reg = round(min(net_hours, ordinary_daily_hours), 2)
        ot = round(max(net_hours - ordinary_daily_hours, 0.0), 2)

        all_flags = tuple(dict.fromkeys(validate_punch_integrity(start, site) + flags)) if site else tuple(flags)
        lines.append(
            PayrollLine(
                employee_id=ev.employee_id,
                site_id=ev.site_id,
                regular_hours=reg,
                overtime_hours=ot,
                break_deducted_min=break_min,
                anomalies=all_flags,
            )
        )

    return lines, dropped


def build_payroll_export_batch(
    pay_period_id: str,
    target_provider: str,
    lines: list[PayrollLine],
) -> dict[str, object]:
    """Produce an idempotent, content-hashed payroll export payload for Xero/iPayroll targets."""
    agg: dict[tuple[str, str], dict[str, float]] = {}
    blocked: list[str] = []

    for line in lines:
        if any(a.startswith("geofence_breach") or a.startswith("unknown_site") for a in line.anomalies):
            blocked.append(f"{line.employee_id}:{','.join(line.anomalies)}")
            continue
        key = (line.employee_id, line.site_id)
        bucket = agg.setdefault(key, {"regular_hours": 0.0, "overtime_hours": 0.0})
        bucket["regular_hours"] = round(bucket["regular_hours"] + line.regular_hours, 2)
        bucket["overtime_hours"] = round(bucket["overtime_hours"] + line.overtime_hours, 2)

    entries = [
        {"employee_id": emp, "site_id": site, **vals}
        for (emp, site), vals in sorted(agg.items())
    ]
    digest_input = json.dumps({"period": pay_period_id, "provider": target_provider, "entries": entries}, sort_keys=True)
    idempotency_key = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]

    return {
        "pay_period_id": pay_period_id,
        "provider": target_provider,
        "idempotency_key": f"{target_provider}:{pay_period_id}:{idempotency_key}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": entries,
        "blocked_review": blocked,
    }
