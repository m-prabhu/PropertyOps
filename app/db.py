"""Schema + seed for PropertyOps.

Target schema is defined in docs/SPEC.md §2. We control the seed and have no
production data, so this replaces the earlier schema outright: POST /api/reset
re-seeds a known world.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .clock import now as clock_now

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "propertyops.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS property (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  name       TEXT NOT NULL,
  timezone   TEXT NOT NULL,
  currency   TEXT NOT NULL,
  room_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
  room_no      TEXT PRIMARY KEY,
  floor        INTEGER NOT NULL,
  category     TEXT NOT NULL CHECK (category IN ('deluxe','premier','suite')),
  status       TEXT NOT NULL CHECK (status IN ('vacant','occupied','blocked','out_of_service')),
  housekeeping TEXT NOT NULL DEFAULT 'clean' CHECK (housekeeping IN ('clean','dirty','inspected')),
  rate_inr     INTEGER NOT NULL,
  features     TEXT NOT NULL DEFAULT '',
  note         TEXT
);

CREATE TABLE IF NOT EXISTS guests (
  guest_id    TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  tier        TEXT NOT NULL CHECK (tier IN ('silver','gold','platinum')),
  phone       TEXT,
  email       TEXT,
  preferences TEXT,
  language    TEXT NOT NULL DEFAULT 'en-IN',
  notes       TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
  res_id         TEXT PRIMARY KEY,
  guest_id       TEXT NOT NULL REFERENCES guests(guest_id),
  room_no        TEXT REFERENCES rooms(room_no),
  arrival_date   TEXT NOT NULL,
  departure_date TEXT NOT NULL,
  arrival_eta    TEXT,
  booked_category TEXT NOT NULL,
  rate_plan      TEXT NOT NULL DEFAULT 'BAR',
  rate_inr       INTEGER NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('expected','in_house','checked_out','no_show','walked'))
);

CREATE TABLE IF NOT EXISTS folio_charges (
  charge_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  res_id      TEXT NOT NULL REFERENCES reservations(res_id),
  category    TEXT NOT NULL,
  description TEXT NOT NULL,
  amount_inr  INTEGER NOT NULL,
  posted_at   TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
  reversed_reason TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at  TEXT NOT NULL,
  department  TEXT NOT NULL,
  location    TEXT NOT NULL,
  priority    TEXT NOT NULL CHECK (priority IN ('emergency','high','medium','low')),
  description TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','done','cancelled')),
  assignee    TEXT,
  sla_due     TEXT NOT NULL,
  closed_at   TEXT,
  note        TEXT
);

CREATE TABLE IF NOT EXISTS comps (
  comp_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at  TEXT NOT NULL,
  guest_id    TEXT NOT NULL REFERENCES guests(guest_id),
  amount_inr  INTEGER NOT NULL,
  reason      TEXT NOT NULL,
  rule_code   TEXT,
  approved_by TEXT,
  request_id  TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS moves (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  guest_id   TEXT NOT NULL,
  from_room  TEXT NOT NULL,
  to_room    TEXT NOT NULL,
  reason     TEXT NOT NULL,
  request_id TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS followups (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  room_no    TEXT NOT NULL,
  due_at     TEXT NOT NULL,
  note       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','missed'))
);

CREATE TABLE IF NOT EXISTS policy_rules (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  code           TEXT NOT NULL UNIQUE,
  topic          TEXT NOT NULL,
  condition_json TEXT NOT NULL,
  limit_inr      INTEGER,
  requires_role  TEXT,
  escalate_to    TEXT,
  message        TEXT NOT NULL,
  priority       INTEGER NOT NULL DEFAULT 10,
  active         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS staff (
  staff_id      TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('front_desk','duty_manager','gm')),
  authority_inr INTEGER NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS approvals (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  guest_id   TEXT NOT NULL,
  amount_inr INTEGER NOT NULL,
  reason     TEXT NOT NULL,
  rule_code  TEXT,
  status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','declined')),
  decided_by TEXT,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS partner_hotels (
  partner_id      TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  area            TEXT NOT NULL,
  distance_km     REAL NOT NULL,
  category        TEXT NOT NULL CHECK (category IN ('deluxe','premier','suite')),
  rate_inr        INTEGER NOT NULL,
  rooms_available INTEGER NOT NULL DEFAULT 0,
  phone           TEXT,
  active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transport_requests (
  transport_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   TEXT NOT NULL,
  guest_id     TEXT NOT NULL,
  partner_id   TEXT NOT NULL,
  pickup_at    TEXT NOT NULL,
  fare_inr     INTEGER NOT NULL,
  status       TEXT NOT NULL DEFAULT 'booked'
);

CREATE TABLE IF NOT EXISTS walks (
  walk_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at   TEXT NOT NULL,
  guest_id     TEXT NOT NULL,
  partner_id   TEXT NOT NULL,
  transport_id INTEGER,
  reason       TEXT NOT NULL,
  approved_by  TEXT NOT NULL,
  cost_inr     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sla_alerts (
  task_id      INTEGER PRIMARY KEY,
  created_at   TEXT NOT NULL,
  minutes_left INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_chunks (
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  body  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,
  entity     TEXT,
  entity_id  TEXT,
  detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_res_room ON reservations(room_no, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, sla_due);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit(created_at);
CREATE INDEX IF NOT EXISTS idx_rules_topic ON policy_rules(topic, active);
"""

# --- seed constants -------------------------------------------------

CATEGORY_BY_FLOOR = {4: "deluxe", 5: "deluxe", 6: "deluxe", 7: "premier", 8: "suite"}
RATE_BY_FLOOR = {4: 6500, 5: 6500, 6: 7000, 7: 9500, 8: 14000}
FEATURES = {
    "deluxe": "queen bed, work desk, city view",
    "premier": "king bed, lounge access, corner room",
    "suite": "separate living area, king bed, panoramic view",
}

# 92.5% occupancy on purpose: nothing free on floor 4, where the incident happens.
VACANT_ROOMS = {"604", "702", "803"}

STAFF = [
    ("S-01", "R. Menon", "duty_manager", 5000),
    ("S-02", "A. Sharma", "gm", 25000),
    ("S-03", "K. Pillai", "front_desk", 750),
]

GUESTS = [
    ("G-1001", "Mr. Arjun Rao", "gold", "+91 98450 11223", "arjun.rao@example.com", "high floor, quiet room"),
    ("G-1002", "Ms. Priya Nair", "platinum", "+91 98860 44556", "priya.nair@example.com", "late checkout, spa"),
    ("G-1003", "Mr. David Fernandes", "silver", "+91 99001 77889", "d.fernandes@example.com", None),
    ("G-1004", "Dr. Meera Iyer", "gold", "+91 94480 22446", "meera.iyer@example.com", "extra pillows"),
    ("G-1005", "Mr. Karthik Reddy", "silver", "+91 90080 66554", "karthik.reddy@example.com", None),
    ("G-2001", "Ms. Ananya Bhat", "silver", "+91 97400 33221", "ananya.bhat@example.com", None),
    ("G-2002", "Mr. Vikram Shetty", "gold", "+91 96320 55447", "vikram.shetty@example.com", "airport pickup"),
    ("G-2003", "Ms. Neha Kulkarni", "silver", "+91 90190 44532", "neha.kulkarni@example.com", None),
]

# Properties we can walk a guest to. Category must be equal or better.
PARTNERS = [
    ("P-01", "Bloom Rooms", "Indiranagar", 2.4, "deluxe", 5200, 8, "+91 80 4123 7788"),
    ("P-02", "The Leela", "Old Madras Road", 1.8, "premier", 12500, 4, "+91 80 2521 1234"),
    ("P-03", "Ginger", "Whitefield", 3.6, "premier", 4800, 12, "+91 80 6663 3333"),
    ("P-04", "Taj", "Yeshwantpur", 6.2, "suite", 22000, 2, "+91 80 6690 0660"),
]

POLICY_RULES = [
    # code, topic, condition, limit, requires_role, escalate_to, message, priority
    ("COMP-SILVER", "comp", {"tier": "silver"}, 750, None, None,
     "Silver guests are covered to 750 INR without further approval.", 10),
    ("COMP-GOLD", "comp", {"tier": "gold"}, 1500, None, None,
     "Gold guests are covered to 1500 INR without further approval.", 10),
    ("COMP-PLAT", "comp", {"tier": "platinum"}, 3000, None, None,
     "Platinum guests are covered to 3000 INR without further approval.", 10),
    ("COMP-LATE-GOLD", "comp", {"tier": "gold", "after_hour": 22}, 2500, None, None,
     "After 10pm a Gold guest can be covered to 2500 INR for a room defect.", 50),
    ("COMP-ESCALATE", "comp", {"amount_gt": 5000}, None, None, "gm",
     "Anything above 5000 INR must go to the General Manager.", 100),
    ("LC-WAIVE", "late_checkout", {}, 0, "duty_manager", None,
     "A late checkout fee is waived only with duty manager sign-off.", 10),
    ("RATE-CHANGE", "rate_change", {}, 0, "duty_manager", None,
     "A mid-stay rate change is a compensation and needs an approver.", 10),
    ("REVERSAL-SMALL", "reversal", {}, 1500, None, None,
     "Reversals up to 1500 INR can be corrected at the desk without further approval.", 10),
    ("ESCALATE-PLAT", "escalation", {"tier": "platinum"}, None, None, "gm",
     "Platinum guests always escalate to the General Manager.", 100),
]

POLICY = [
    ("room_move",
     "STANDARD ROOM MOVE. Prefer the same category on the same floor. If that is "
     "unavailable, offer the nearest floor in the same category. A move into a higher "
     "category keeps the guest's booked rate; the difference is recorded as a compensation."),
    ("comp_limits",
     "COMPENSATION AUTHORITY. Per incident, without further approval: Silver 750 INR, "
     "Gold 1500 INR, Platinum 3000 INR. After 10pm a Gold guest with a room defect may be "
     "covered to 2500 INR. Anything above the ceiling needs a named approver, and anything "
     "above 5000 INR goes to the General Manager."),
    ("comp_eligibility",
     "WHEN A COMP IS JUSTIFIED. (a) the room has a defect staff cannot resolve within 30 "
     "minutes, (b) the guest has already waited more than 45 minutes, (c) the guest was "
     "moved to a lower category than booked."),
    ("out_of_service",
     "OUT OF SERVICE ROOMS. A room with an unresolved maintenance defect must be set to "
     "out_of_service and must not be resold until maintenance closes the task."),
    ("task_sla",
     "TASK SLA. Emergency 15 minutes, high 30 minutes, medium 120 minutes, low 240 minutes. "
     "Housekeeping room recovery is always high priority."),
    ("late_checkout",
     "LATE CHECKOUT. A late checkout can only extend as far as the next arrival's "
     "estimated time on that room. Never promise a departure later than a same-day "
     "arrival. If the guest needs longer, offer a move to a vacant room instead."),
    ("escalation",
     "ESCALATION TO GENERAL MANAGER. Escalate if the compensation exceeds 5000 INR, the "
     "guest is Platinum, the guest threatens a public review, or the incident is a safety issue."),
    ("rate_change",
     "RATE CHANGES. Never edit a guest's rate directly. A mid-stay rate reduction is "
     "recorded as a compensation with a reason and an approver."),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return clock_now().strftime("%Y-%m-%dT%H:%M:%S")


def iso_in(minutes: int) -> str:
    return (clock_now() + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def seed(conn: sqlite3.Connection) -> None:
    for table in ("property", "rooms", "guests", "reservations", "folio_charges", "tasks",
                  "comps", "moves", "followups", "policy_rules", "staff", "policy_chunks",
                  "audit", "approvals", "partner_hotels", "transport_requests", "walks",
                  "sla_alerts"):
        conn.execute(f"DELETE FROM {table}")

    conn.execute(
        "INSERT INTO property (id, name, timezone, currency, room_count) VALUES (1,?,?,?,?)",
        ("Polaris Business Hotel", "Asia/Kolkata", "INR", 40),
    )

    for floor in sorted(CATEGORY_BY_FLOOR):
        category = CATEGORY_BY_FLOOR[floor]
        for n in range(1, 9):
            room_no = f"{floor}{n:02d}"
            conn.execute(
                "INSERT INTO rooms (room_no, floor, category, status, housekeeping, rate_inr, features) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    room_no, floor, category,
                    "vacant" if room_no in VACANT_ROOMS else "occupied",
                    "clean",
                    RATE_BY_FLOOR[floor],
                    FEATURES[category],
                ),
            )

    conn.executemany(
        "INSERT INTO guests (guest_id, name, tier, phone, email, preferences) VALUES (?,?,?,?,?,?)",
        GUESTS,
    )
    conn.executemany(
        "INSERT INTO staff (staff_id, name, role, authority_inr) VALUES (?,?,?,?)", STAFF
    )

    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    in_two = (today + timedelta(days=2)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()

    in_house = [
        ("R-9001", "G-1001", "408"),   # the incident room
        ("R-9002", "G-1002", "508"),   # W2: has a same-day arrival at 14:00
        ("R-9003", "G-1003", "401"),
        ("R-9004", "G-1004", "701"),
        ("R-9005", "G-1005", "802"),
    ]
    for res_id, guest_id, room_no in in_house:
        conn.execute(
            "INSERT INTO reservations (res_id, guest_id, room_no, arrival_date, departure_date, "
            "arrival_eta, booked_category, rate_inr, status) VALUES (?,?,?,?,?,?,?,?, 'in_house')",
            (res_id, guest_id, room_no, yesterday, today.isoformat(), None,
             CATEGORY_BY_FLOOR[int(room_no[0])], RATE_BY_FLOOR[int(room_no[0])]),
        )

    # Expected arrivals: these make W2 a real conflict rather than a yes/no.
    conn.execute(
        "INSERT INTO reservations (res_id, guest_id, room_no, arrival_date, departure_date, "
        "arrival_eta, booked_category, rate_inr, status) VALUES (?,?,?,?,?,?,?,?, 'expected')",
        ("R-9101", "G-2001", "508", tomorrow, in_two, "14:00", "deluxe", 6500),
    )
    conn.execute(
        "INSERT INTO reservations (res_id, guest_id, room_no, arrival_date, departure_date, "
        "arrival_eta, booked_category, rate_inr, status) VALUES (?,?,?,?,?,?,?,?, 'expected')",
        ("R-9102", "G-2002", "604", tomorrow, in_two, "14:00", "deluxe", 7000),
    )

    # An arrival tonight with no room assigned: the guest a walk would apply to.
    conn.execute(
        "INSERT INTO reservations (res_id, guest_id, room_no, arrival_date, departure_date, "
        "arrival_eta, booked_category, rate_inr, status) VALUES (?,?,?,?,?,?,?,?, 'expected')",
        ("R-9103", "G-2003", None, today.isoformat(), in_two, "23:30", "deluxe", 6500),
    )

    conn.executemany(
        "INSERT INTO partner_hotels (partner_id, name, area, distance_km, category, rate_inr, "
        "rooms_available, phone) VALUES (?,?,?,?,?,?,?,?)", PARTNERS,
    )

    conn.execute(
        "INSERT INTO folio_charges (res_id, category, description, amount_inr, posted_at) "
        "VALUES ('R-9001','room','Room charge 408',6500,?)", (now_iso(),)
    )
    conn.execute(
        "INSERT INTO folio_charges (res_id, category, description, amount_inr, posted_at) "
        "VALUES ('R-9001','minibar','Minibar - 2 soft drinks',600,?)", (now_iso(),)
    )

    conn.executemany(
        "INSERT INTO tasks (created_at, department, location, priority, description, sla_due, assignee) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (now_iso(), "housekeeping", "Pool deck", "low", "Restock towels and sun loungers", iso_in(240), "M. Das"),
            (now_iso(), "maintenance", "Lobby", "medium", "Replace lobby AC filter (scheduled)", iso_in(120), "S. Kumar"),
            # Already close to breaching, so the shift opens with a live alert.
            (iso_in(-25), "maintenance", "605", "high", "Guest reports shower drain blocked", iso_in(5), "S. Kumar"),
        ],
    )

    # A little housekeeping variety so the board shows the full state model.
    for room_no, state in (("803", "dirty"), ("702", "inspected")):
        conn.execute("UPDATE rooms SET housekeeping = ? WHERE room_no = ?", (state, room_no))

    conn.executemany(
        "INSERT INTO policy_rules (code, topic, condition_json, limit_inr, requires_role, "
        "escalate_to, message, priority) VALUES (?,?,?,?,?,?,?,?)",
        [
            (code, topic, json.dumps(cond), limit, req, esc, msg, prio)
            for code, topic, cond, limit, req, esc, msg, prio in POLICY_RULES
        ],
    )
    conn.executemany("INSERT INTO policy_chunks (topic, body) VALUES (?,?)", POLICY)

    conn.execute(
        "INSERT INTO audit (created_at, actor, action, entity, entity_id, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), "system", "shift_start", "property", "1",
         json.dumps({"detail": "Shift started. Duty manager on the desk."})),
    )


def init(reset: bool = False) -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) AS c FROM rooms").fetchone()["c"] == 0
        if reset or empty:
            seed(conn)
        conn.commit()
    finally:
        conn.close()
