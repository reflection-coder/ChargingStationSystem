# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application (starts at http://localhost:5000)
python app.py

# Run integration tests
python test_system.py

# Run fault-handling / time-order dispatch tests
python test_time_order_dispatch.py
```

Default admin credentials: `admin` / `admin123`. The SQLite database (`charging_station.db`) is auto-created on first run.

## Architecture

This is a Flask-based smart charging station scheduling system. The codebase follows a layered MVC structure:

```
app.py (routes/controllers)
    ├── scheduler.py  (core engine, background thread)
    ├── billing.py    (pricing logic)
    ├── settings.py   (runtime config, singleton)
    └── database.py   (SQLite CRUD, all persistence)
```

### Core Modules

**`scheduler.py`** — Central orchestration engine running in a background thread. Manages:
- Queue number generation (FIFO: `F1`, `F2`... fast; `T1`, `T2`... slow)
- Waiting area (default 10-vehicle capacity) and per-charger queues (3 vehicles each)
- Scheduling algorithm: "Shortest Completion Time" — assigns vehicles to the charger with minimum `wait_time + own_charging_time`
- Fault handling with two strategies: *priority-based* (redistribute to same-type chargers) and *time-order* (merge all pending and re-queue chronologically)
- Simulation time (completely independent from real time, controllable by admin at configurable speedup)

**`billing.py`** — Dynamic, segment-aware pricing. Charging sessions spanning multiple time periods are split and billed per segment:
- Peak (10:00–15:00, 18:00–21:00): 1.0 yuan/kWh
- Normal (7:00–10:00, 15:00–18:00, 21:00–23:00): 0.7 yuan/kWh
- Valley (23:00–7:00): 0.4 yuan/kWh
- Plus a fixed service fee (default 0.8 yuan/kWh)

**`database.py`** — All SQLite3 access. Tables: `users`, `chargers`, `requests`, `bills`, `system_logs`, `system_settings`. No ORM — raw SQL with `sqlite3` module.

**`settings.py`** — Singleton `SettingsManager`. All runtime parameters (charger counts, power ratings, queue sizes, pricing, simulation speed) are stored here and persisted to `system_settings` table. Updates take effect without restart and auto-sync charger records.

**`app.py`** — Flask routes only. Uses `@login_required` / `@admin_required` decorators. User routes under `/user/*`, admin routes under `/admin/*`, AJAX endpoints under `/api/*`.

### Key Data Flow

1. User submits charging request → `scheduler.submit_request()` → creates DB record (status=`waiting`), generates queue number, calls `trigger_scheduling()`
2. Scheduler's "shortest completion time" algorithm assigns request to a charger queue (status=`queued`)
3. Background thread advances simulation time; when a vehicle reaches position 0 it begins charging (status=`charging`)
4. On completion: `billing.py` generates a segmented bill, charger stats updated, next vehicle promoted

### Request Statuses
`waiting` → `queued` → `charging` → `completed` (or `cancelled` at any pre-completion stage)

### Charger Statuses
`working` | `stopped` | `fault`

## Configuration

Static defaults live in `config.py` (fast chargers: 3 × 30 kW, slow chargers: 2 × 10 kW). These are overridden at runtime via `SettingsManager` / the admin settings panel. Never read `config.py` constants directly in business logic — always go through `settings.py`.
