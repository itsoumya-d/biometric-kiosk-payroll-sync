# Biometric & Kiosk Attendance Payroll Sync

Offline-resilient clock-in event ingestion, geofenced mobile reconciliation, and idempotent payroll batch export engine for multi-site workforce platforms.

## Problem

Field teams, warehouses, and retail venues capture clock-in events from both tablet/biometric kiosks in patchy connectivity zones and GPS-enabled mobile apps. Without deterministic event handling, teams hit three recurring operational failures:

1. **Duplicate bounce punches** when staff tap a badge twice or a kiosk replays buffered offline events after reconnect.
2. **Unsynchronized terminal clocks & buddy-clocking** when mobile punches fall outside site geofences or kiosk NTP drift exceeds tolerance.
3. **Double-posted payroll exports** when re-running pay runs across Xero/iPayroll-style accounting targets.

## Running Tests

```bash
PYTHONPATH=src python3 -m pytest -v
```
