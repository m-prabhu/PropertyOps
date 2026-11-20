"""The 14 P0 tools.

Every tool returns the same envelope (docs/SPEC.md Â§4):
    {ok, status, message, data, hints}

`message` is written for speech. `data` is for the console and the model's
reasoning. Failures always carry a next step, because that is what lets the agent
recover in one turn instead of apologising.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from . import policy
from .clock import now as clock_now
from .db import connect, now_iso

CATEGORY_RANK = {"deluxe": 1, "premier": 2, "suite": 3}
SLA_MINUTES = {"emergency": 15, "high": 30, "medium": 120, "low": 240}
ROOM_STATUSES = {"vacant", "occupied", "blocked", "out_of_service"}
TASK_STATUSES = {"open", "in_progress", "done", "cancelled"}

# Comps at or below this clear without a name. Anything above it needs a human,
# even when it sits inside the guest's ceiling: the ceiling says how much the desk
# may spend, not that the agent may decide to spend it.
AUTO_COMP_LIMIT = 500


# --- envelope --------------------------------------------------------

def ok(message: str, data: dict | None = None, status: str = "DONE",
       hints: dict | None = None) -> dict:
    return {"ok": True, "status": status, "message": message,
            "data": data or {}, "hints": hints or {}}


def fail(status: str, message: str, data: dict | None = None,
         hints: dict | None = None) -> dict:
    return {"ok": False, "status": status, "message": message,
            "data": data or {}, "hints": hints or {}}


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _audit(conn, action: str, entity: str | None, entity_id: str | None,
           detail: dict, actor: str = "agent") -> None:
    conn.execute(
        "INSERT INTO audit (created_at, actor, action, entity, entity_id, detail_json) "
        "VALUES (?,?,?,?,?,?)",
        (now_iso(), actor, action, entity, entity_id, json.dumps(detail)),
    )


def _rows(cur) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _minutes_until(iso_ts: str) -> int:
    try:
        return int((datetime.fromisoformat(iso_ts) - clock_now()).total_seconds() // 60)
    except (TypeError, ValueError):
        return 0


def _guest_or_none(conn, room_no: str):
    return conn.execute(
        """SELECT r.res_id, r.guest_id, r.departure_date, g.name, g.tier, g.phone
           FROM reservations r JOIN guests g ON g.guest_id = r.guest_id
           WHERE r.room_no = ? AND r.status = 'in_house'""",
        (room_no,),
    ).fetchone()


# --- 1..7 read tools -------------------------------------------------

def getShiftSummary(scope: str = "shift") -> dict:
    """What happened this shift, what is still open, what is about to breach."""
    with db() as conn:
        moves = _rows(conn.execute(
            "SELECT created_at, guest_id, from_room, to_room, reason FROM moves "
            "ORDER BY id DESC LIMIT 5"))
        comps = _rows(conn.execute(
            "SELECT c.amount_inr, c.approved_by, g.name FROM comps c "
            "JOIN guests g ON g.guest_id = c.guest_id ORDER BY c.comp_id DESC LIMIT 5"))
        comp_total = conn.execute("SELECT COALESCE(SUM(amount_inr),0) s FROM comps").fetchone()["s"]
        tasks = _rows(conn.execute(
            "SELECT task_id, department, location, priority, description, sla_due "
            "FROM tasks WHERE status IN ('open','in_progress')"))
        followups = _rows(conn.execute(
            "SELECT room_no, due_at, note FROM followups WHERE status = 'pending' LIMIT 5"))
        oos = _rows(conn.execute(
            "SELECT room_no FROM rooms WHERE status = 'out_of_service'"))
        walks = _rows(conn.execute(
            "SELECT w.created_at, g.name, p.name AS partner, w.approved_by, w.cost_inr "
            "FROM walks w JOIN guests g ON g.guest_id = w.guest_id "
            "JOIN partner_hotels p ON p.partner_id = w.partner_id "
            "ORDER BY w.walk_id DESC LIMIT 3"))
        total = conn.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"]
        occupied = conn.execute(
            "SELECT COUNT(*) c FROM rooms WHERE status = 'occupied'").fetchone()["c"]

    for t in tasks:
        t["minutes_to_sla"] = _minutes_until(t["sla_due"])
    tasks.sort(key=lambda t: t["minutes_to_sla"])
    at_risk = [t for t in tasks if t["minutes_to_sla"] <= 30]

    parts = []
    if moves:
        parts.append(f"{len(moves)} room move{'s' if len(moves) > 1 else ''} this shift")
    if comps:
        parts.append(f"{len(comps)} compensation{'s' if len(comps) > 1 else ''} totalling {comp_total} rupees")
    if at_risk:
        first = at_risk[0]
        parts.append(
            f"{len(at_risk)} task{'s' if len(at_risk) > 1 else ''} close to SLA breach: "
            f"{first['description']} ({first['department']}, {first['location']}), "
            f"{first['minutes_to_sla']} minutes left")
    if oos:
        parts.append("out of service: " + ", ".join(r["room_no"] for r in oos))
    if walks:
        first_walk = walks[0]
        parts.append(
            f"{len(walks)} guest{'s' if len(walks) > 1 else ''} walked, "
            f"most recently {first_walk['name']} to {first_walk['partner']}")
    if followups:
        parts.append(f"{len(followups)} follow-up{'s' if len(followups) > 1 else ''} pending")

    message = ("Nothing open. The property is quiet." if not parts
               else "; ".join(parts) + ".")

    return ok(message, {
        "occupancy_pct": round(occupied / total * 100, 1) if total else 0,
        "moves": moves, "comps": comps, "comp_total_inr": comp_total,
        "tasks": tasks[:6], "tasks_at_risk": len(at_risk),
        "followups": followups, "out_of_service": [r["room_no"] for r in oos],
        "walks": walks,
    })


def getRoom(room_no: str) -> dict:
    with db() as conn:
        room = conn.execute("SELECT * FROM rooms WHERE room_no = ?",
                            (str(room_no).strip(),)).fetchone()
        if room is None:
            return fail(
                "NOT_FOUND",
                f"No room called {room_no}. Rooms run {_room_number_ranges(conn)}.",
                {"asked_for": str(room_no).strip()},
                hints={"next": ["searchGuest", "getAvailabilitySummary"],
                       "note": "Do not retry the same number. Read it back, or search "
                               "by the guest's name instead."})
        guest = _guest_or_none(conn, room["room_no"])
        tasks = _rows(conn.execute(
            "SELECT task_id, department, priority, description, status, sla_due, assignee "
            "FROM tasks WHERE location = ? AND status IN ('open','in_progress') "
            "ORDER BY sla_due", (room["room_no"],)))

    message = (f"Room {room['room_no']}, {room['category']} on floor {room['floor']}, "
               f"{room['status'].replace('_', ' ')}.")
    if guest:
        message += f" {guest['name']} is in house."
    if tasks:
        first = tasks[0]
        message += (f" Open task: {first['description']}"
                    f" ({first['department']}, {first['priority']}).")
        if len(tasks) > 1:
            message += f" Plus {len(tasks) - 1} more."

    return ok(message, {
        "room": dict(room),
        "guest": {"guest_id": guest["guest_id"], "name": guest["name"],
                  "tier": guest["tier"]} if guest else None,
        "tasks": tasks,
    })


def findAvailableRooms(category: str, floor: int | None = None,
                         features: str | None = None) -> dict:
    category = str(category).strip().lower()
    if category not in CATEGORY_RANK:
        return fail("ERROR", f"I do not have a room category called {category}.",
                    hints={"next": ["findAvailableRooms"], "valid": list(CATEGORY_RANK)})

    with db() as conn:
        vacant = _rows(conn.execute(
            "SELECT room_no, floor, category, rate_inr, features FROM rooms "
            "WHERE status = 'vacant' ORDER BY floor, room_no"))
        total = conn.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"]
        occupied = conn.execute(
            "SELECT COUNT(*) c FROM rooms WHERE status = 'occupied'").fetchone()["c"]

    if features:
        needle = str(features).lower()
        vacant = [r for r in vacant if needle in r["features"].lower()]

    same = [r for r in vacant if r["category"] == category]
    upgrades = [r for r in vacant if CATEGORY_RANK[r["category"]] > CATEGORY_RANK[category]]

    on_floor, elsewhere = [], same
    if floor:
        try:
            wanted = int(floor)
        except (TypeError, ValueError):
            wanted = None
        if wanted:
            on_floor = [r for r in same if r["floor"] == wanted]
            elsewhere = sorted([r for r in same if r["floor"] != wanted],
                               key=lambda r: abs(r["floor"] - wanted))

    if not same and not upgrades:
        return fail("UNAVAILABLE",
                    f"Nothing free in {category} or above right now.",
                    {"occupancy_pct": round(occupied / total * 100, 1) if total else 0},
                    hints={"next": ["findAvailableRooms", "getArrivals"]})

    if same:
        if floor and not on_floor:
            nearest = elsewhere[0]
            message = (f"Nothing free in {category} on floor {floor}. "
                       f"{nearest['room_no']} is the nearest, on floor {nearest['floor']}.")
        else:
            first = (on_floor or elsewhere or same)[0]
            message = f"{first['room_no']} is free in {category}, floor {first['floor']}."
    else:
        cheapest = sorted(upgrades, key=lambda r: r["rate_inr"])[0]
        message = (f"Nothing free in {category}. The cheapest upgrade is "
                   f"{cheapest['room_no']}, {cheapest['category']} on floor "
                   f"{cheapest['floor']} at {cheapest['rate_inr']} rupees.")

    return ok(message, {
        "requested_category": category,
        "matches_on_requested_floor": on_floor[:3],
        "same_category_elsewhere": elsewhere[:3],
        "upgrade_options": sorted(upgrades, key=lambda r: r["rate_inr"])[:2],
        "occupancy_pct": round(occupied / total * 100, 1) if total else 0,
    })


def setRoomStatus(room_no: str, status: str, reason: str) -> dict:
    status = str(status).strip().lower()
    if status not in ROOM_STATUSES:
        return fail("ERROR", f"{status} is not a room status I can set.",
                    hints={"valid": sorted(ROOM_STATUSES)})

    with db() as conn:
        room = conn.execute("SELECT * FROM rooms WHERE room_no = ?",
                            (str(room_no).strip(),)).fetchone()
        if room is None:
            return fail("NOT_FOUND", f"No room called {room_no}.")

        if status == "vacant":
            open_tasks = _rows(conn.execute(
                "SELECT task_id FROM tasks WHERE location = ? AND status IN ('open','in_progress')",
                (room["room_no"],)))
            if open_tasks:
                return fail("CONFLICT",
                            f"{room['room_no']} still has an open defect task, so it cannot go back on sale.",
                            {"open_task_ids": [t["task_id"] for t in open_tasks]},
                            hints={"next": ["updateTask", "setRoomStatus"]})

        conn.execute("UPDATE rooms SET status = ?, note = ? WHERE room_no = ?",
                     (status, reason, room["room_no"]))
        _audit(conn, "room_status", "room", room["room_no"],
               {"from": room["status"], "to": status, "reason": reason})
        return ok(f"{room['room_no']} is now {status.replace('_', ' ')}.",
                  {"room_no": room["room_no"], "from": room["status"], "to": status})


def _room_number_ranges(conn) -> str:
    """A compact statement of which room numbers exist, for NOT_FOUND replies."""
    floors: dict[str, list[int]] = {}
    for row in conn.execute("SELECT room_no FROM rooms"):
        room_no = row["room_no"]
        floors.setdefault(room_no[0], []).append(int(room_no[1:]))
    return ", ".join(
        f"{floor}01 to {floor}{max(numbers):02d}"
        for floor, numbers in sorted(floors.items()))


def getGuestContext(room_no: str) -> dict:
    with db() as conn:
        room = conn.execute("SELECT * FROM rooms WHERE room_no = ?",
                            (str(room_no).strip(),)).fetchone()
        if room is None:
            return fail(
                "NOT_FOUND",
                f"No room called {room_no}. Rooms run {_room_number_ranges(conn)}.",
                {"asked_for": str(room_no).strip()},
                hints={"next": ["searchGuest", "getAvailabilitySummary"],
                       "note": "Do not retry the same number. Read it back, or search "
                               "by the guest's name instead."})

        res = _guest_or_none(conn, room["room_no"])
        if res is None:
            return ok(f"{room['room_no']} has no guest in house.",
                      {"room": dict(room), "guest": None})
        comps = conn.execute(
            "SELECT COALESCE(SUM(amount_inr),0) s FROM comps WHERE guest_id = ?",
            (res["guest_id"],)).fetchone()["s"]
        past = conn.execute(
            "SELECT COUNT(*) c FROM reservations WHERE guest_id = ?", (res["guest_id"],)
        ).fetchone()["c"]
        tasks = _rows(conn.execute(
            "SELECT task_id, department, priority, description FROM tasks "
            "WHERE location = ? AND status IN ('open','in_progress')", (room["room_no"],)))

    ceiling = policy.tier_ceiling(res["tier"])

    return ok(
        f"{res['name']}, {res['tier']} tier, in {room['room_no']} at {room['rate_inr']} rupees a night "
        f"until {res['departure_date']}.",
        {"room": dict(room),
         "guest": {
             "guest_id": res["guest_id"], "name": res["name"], "tier": res["tier"],
             "phone": res["phone"], "checked_out_on": res["departure_date"],
             "rate_inr": room["rate_inr"], "stays_to_date": past,
             "comps_to_date_inr": comps,
             "comp_ceiling_inr": ceiling["limit_inr"], "ceiling_rule": ceiling["rule_code"],
         },
         "open_tasks": tasks},
    )


HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "shri", "smt", "sri", "mister"}


def _name_tokens(value: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", str(value).lower()) if t not in HONORIFICS]


def searchGuest(query: str) -> dict:
    """Match "Mr Rao", "rao", "arjun" and phone fragments against the guest list."""
    q = str(query).strip()
    tokens = _name_tokens(q)
    digits = re.sub(r"\D", "", q)

    with db() as conn:
        guests = _rows(conn.execute(
            "SELECT guest_id, name, tier, phone FROM guests"))

    exact = []
    for guest in guests:
        name_tokens = _name_tokens(guest["name"])
        phone_digits = re.sub(r"\D", "", guest["phone"] or "")
        if tokens and all(t in name_tokens for t in tokens):
            exact.append(guest)
        elif tokens and len(digits) >= 4 and digits in phone_digits:
            exact.append(guest)

    if exact:
        return ok(f"Found {exact[0]['name']}." if len(exact) == 1
                  else f"{len(exact)} guests match.",
                  {"matches": exact[:5]})

    # Nothing exact: offer the closest name rather than a bare NOT_FOUND.
    near = [g for g in guests
            if tokens and any(t in _name_tokens(g["name"]) for t in tokens)]
    if near:
        return ok(f"No exact match. Did you mean {near[0]['name']}?",
                  {"matches": near[:5], "exact": False})

    return fail("NOT_FOUND", f"No guest matching {query}.",
                hints={"next": ["getGuestContext", "getShiftSummary"]})


def getArrivals(room_no: str | None = None, date: str | None = None) -> dict:
    day = date or clock_now().date().isoformat()
    sql = ("SELECT r.res_id, r.room_no, r.arrival_date, r.arrival_eta, r.booked_category, "
           "g.name, g.tier FROM reservations r JOIN guests g ON g.guest_id = r.guest_id "
           "WHERE r.status = 'expected' AND r.arrival_date = ?")
    params: list[Any] = [day]
    if room_no:
        sql += " AND r.room_no = ?"
        params.append(str(room_no).strip())
    with db() as conn:
        rows = _rows(conn.execute(sql + " ORDER BY r.arrival_eta", params))

    if not rows:
        return ok(f"No arrivals expected on {day}." + (f" for {room_no}." if room_no else ""),
                  {"date": day, "arrivals": []})
    first = rows[0]
    return ok(f"{len(rows)} arrival{'s' if len(rows) > 1 else ''} expected on {day}; "
              f"{first['name']} at {first['arrival_eta']} into {first['room_no']}.",
              {"date": day, "arrivals": rows})


# --- 8..14 write tools ----------------------------------------------

def moveRoom(guest_id: str, from_room: str, to_room: str, reason: str,
              request_id: str | None = None) -> dict:
    with db() as conn:
        if request_id:
            prior = conn.execute("SELECT * FROM moves WHERE request_id = ?",
                                 (request_id,)).fetchone()
            if prior:
                return ok("That move is already recorded.", {"move_id": prior["id"]}, "DONE")

        target = conn.execute("SELECT * FROM rooms WHERE room_no = ?",
                              (str(to_room).strip(),)).fetchone()
        if target is None:
            return fail("NOT_FOUND", f"No room called {to_room}.")
        if target["status"] != "vacant":
            free = _rows(conn.execute(
                "SELECT room_no, floor, category FROM rooms WHERE status = 'vacant'"))
            return fail("UNAVAILABLE", f"{target['room_no']} is not vacant.",
                        {"available_now": free},
                        hints={"next": ["findAvailableRooms"]})

        res = conn.execute(
            "SELECT res_id FROM reservations WHERE guest_id = ? AND status = 'in_house'",
            (guest_id,)).fetchone()
        if res is None:
            return fail("NOT_FOUND", f"No in-house reservation for {guest_id}.")

        conn.execute("UPDATE reservations SET room_no = ? WHERE res_id = ?",
                     (target["room_no"], res["res_id"]))
        conn.execute("UPDATE rooms SET status = 'occupied' WHERE room_no = ?",
                     (target["room_no"],))
        conn.execute("UPDATE rooms SET status = 'out_of_service' WHERE room_no = ?",
                     (from_room,))
        cur = conn.execute(
            "INSERT INTO moves (created_at, guest_id, from_room, to_room, reason, request_id) "
            "VALUES (?,?,?,?,?,?)",
            (now_iso(), guest_id, from_room, target["room_no"], reason, request_id))
        _audit(conn, "room_moved", "reservation", res["res_id"],
               {"from": from_room, "to": target["room_no"], "reason": reason})

        return ok(f"Moved to {target['room_no']}. {from_room} is out of service.",
                  {"move_id": cur.lastrowid, "to_room": target["room_no"],
                   "to_floor": target["floor"], "from_room_status": "out_of_service"})


def extendStay(guest_id: str, new_departure: str, rate_inr: int | None = None,
                request_id: str | None = None) -> dict:
    """Late checkout, capped at the next arrival's ETA. Never promise past an arrival."""
    raw = str(new_departure).strip()
    try:
        requested = (datetime.fromisoformat(raw) if "T" in raw
                     else datetime.fromisoformat(raw + "T23:59"))
    except ValueError:
        return fail("ERROR", "Give me the new departure as a date, optionally with a time.")

    with db() as conn:
        if request_id:
            prior = conn.execute(
                "SELECT * FROM reservations WHERE res_id = ? AND departure_date = ?",
                (request_id, requested.date().isoformat())).fetchone()
            if prior:
                return ok("That extension is already recorded.", {"res_id": prior["res_id"]})

        res = conn.execute(
            "SELECT * FROM reservations WHERE guest_id = ? AND status = 'in_house'",
            (guest_id,)).fetchone()
        if res is None:
            return fail("NOT_FOUND", f"No in-house reservation for {guest_id}.")
        if res["room_no"] is None:
            return fail("CONFLICT", "That guest has no room assigned yet.")

        arrival = conn.execute(
            "SELECT res_id, arrival_eta, guest_id FROM reservations "
            "WHERE room_no = ? AND status = 'expected' AND arrival_date = ? "
            "AND arrival_eta IS NOT NULL",
            (res["room_no"], requested.date().isoformat()),
        ).fetchone()

        if arrival and arrival["arrival_eta"]:
            eta = datetime.fromisoformat(f"{requested.date().isoformat()}T{arrival['arrival_eta']}")
            if requested > eta:
                latest = f"{requested.date().isoformat()}T{arrival['arrival_eta']}"
                return fail(
                    "CONFLICT",
                    f"{res['room_no']} is needed for an arrival at {arrival['arrival_eta']}. "
                    f"The latest I can hold it is {arrival['arrival_eta']}.",
                    {"latest_possible_checkout": latest,
                     "room_no": res["room_no"],
                     "arriving_res_id": arrival["res_id"]},
                    hints={"next": ["findAvailableRooms", "extendStay", "applyComp"]})

        conn.execute("UPDATE reservations SET departure_date = ?, rate_inr = COALESCE(?, rate_inr) "
                     "WHERE res_id = ?",
                     (requested.date().isoformat(), rate_inr, res["res_id"]))
        _audit(conn, "stay_extended", "reservation", res["res_id"],
               {"new_departure": requested.isoformat(), "rate_inr": rate_inr})
        return ok(f"Extended to {requested.strftime('%d %b, %H:%M')}.",
                  {"res_id": res["res_id"], "new_departure": requested.isoformat(),
                   "rate_inr": rate_inr})


def applyComp(guest_id: str, amount_inr: int, reason: str,
               approved_by: str | None = None, request_id: str | None = None) -> dict:
    try:
        amount = int(amount_inr)
    except (TypeError, ValueError):
        return fail("ERROR", "I need the compensation as a whole number of rupees.")
    if amount <= 0:
        return fail("ERROR", "A compensation has to be a positive amount.",
                    {"requested_inr": amount})

    with db() as conn:
        if request_id:
            prior = conn.execute("SELECT * FROM comps WHERE request_id = ?",
                                 (request_id,)).fetchone()
            if prior:
                return ok("That compensation is already recorded.",
                          {"comp_id": prior["comp_id"],
                           "approved_by": prior["approved_by"]})
        guest = conn.execute("SELECT name, tier FROM guests WHERE guest_id = ?",
                             (guest_id,)).fetchone()
        if guest is None:
            return fail("NOT_FOUND", f"I do not have a guest {guest_id}.")
        pending = conn.execute(
            "SELECT id, amount_inr FROM approvals WHERE guest_id = ? AND status = 'pending' "
            "ORDER BY id LIMIT 1", (guest_id,)).fetchone()

    # A pending request may not be worked around by quietly lowering the amount.
    # Changing the figure is itself a decision the manager has to make.
    if pending and int(pending["amount_inr"]) != amount and not approved_by:
        return fail(
            "APPROVAL_REQUIRED",
            f"There is a {pending['amount_inr']} rupee request waiting. Changing it to "
            f"{amount} rupees needs the manager to approve that amount by name.",
            {"pending_inr": pending["amount_inr"], "requested_inr": amount,
             "limit_inr": None, "required_role": "duty_manager",
             "rule_code": "PENDING-CHANGE"},
            hints={"next": ["applyComp", "declineApproval"], "needs": "approved_by"})

    decision = policy.evaluate("comp", {
        "tier": guest["tier"], "amount_inr": amount,
        "local_time": clock_now(), "approved_by": approved_by,
    })

    if decision["decision"] in ("ALLOW", "ALLOW_APPROVED"):
        # Above the automatic limit, money only moves with a human name on it.
        if amount > AUTO_COMP_LIMIT and not approved_by:
            return fail(
                "APPROVAL_REQUIRED",
                f"{amount} rupees needs a named approver. Compensations above "
                f"{AUTO_COMP_LIMIT} rupees never clear themselves.",
                {"requested_inr": amount, "auto_limit_inr": AUTO_COMP_LIMIT,
                 "limit_inr": decision.get("limit_inr"),
                 "required_role": policy.required_role(amount),
                 "rule_code": decision["rule_code"]},
                hints={"next": ["applyComp"], "needs": "approved_by"})

        approver = None
        if amount > AUTO_COMP_LIMIT:
            approver = decision.get("approved_by") or approved_by or None
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO comps (created_at, guest_id, amount_inr, reason, rule_code, "
                "approved_by, request_id) VALUES (?,?,?,?,?,?,?)",
                (now_iso(), guest_id, amount, reason, decision["rule_code"],
                 approver, request_id))
            if approver:
                # A request is only closed by a human name: an override, or an
                # explicit decision on a changed amount.
                conn.execute(
                    "UPDATE approvals SET status = 'approved', decided_by = ?, decided_at = ? "
                    "WHERE guest_id = ? AND status = 'pending'",
                    (approver, now_iso(), guest_id))
            _audit(conn, "comp_applied", "guest", guest_id,
                   {"amount_inr": amount, "reason": reason, "rule_code": decision["rule_code"],
                    "approved_by": approver})
        who = approver
        tail = f" Approved by {who}." if who else " Within the guest's ceiling."
        return ok(f"Compensation of {amount} rupees recorded for {guest['name']}." + tail,
                  {"comp_id": cur.lastrowid, "amount_inr": amount,
                   "rule_code": decision["rule_code"], "approved_by": who})

    if decision["decision"] == "ESCALATE":
        with db() as conn:
            _audit(conn, "comp_escalated", "guest", guest_id,
                   {"amount_inr": amount, "reason": reason, "rule": decision["rule_code"]})
        return fail("ESCALATE",
                    f"{amount} rupees is above what this desk can approve. "
                    f"It has to go to the General Manager.",
                    {"amount_inr": amount, "escalate_to": decision["escalate_to"],
                     "rule_code": decision["rule_code"]},
                    hints={"next": ["getShiftSummary"]})

    with db() as conn:
        _audit(conn, "comp_blocked", "guest", guest_id,
               {"amount_inr": amount, "reason": reason, "rule": decision["rule_code"],
                "required_role": decision["required_role"]})
        pending = conn.execute(
            "SELECT id FROM approvals WHERE guest_id = ? AND amount_inr = ? AND status = 'pending'",
            (guest_id, amount)).fetchone()
        if pending:
            approval_id = pending["id"]
        else:
            cur = conn.execute(
                "INSERT INTO approvals (created_at, guest_id, amount_inr, reason, rule_code) "
                "VALUES (?,?,?,?,?)",
                (now_iso(), guest_id, amount, reason, decision["rule_code"]))
            approval_id = cur.lastrowid
    return fail("APPROVAL_REQUIRED", decision["message"],
                {"approval_id": approval_id, "requested_inr": amount,
                 "limit_inr": decision["limit_inr"],
                 "required_role": decision["required_role"],
                 "rule_code": decision["rule_code"], "guest_name": guest["name"]},
                hints={"next": ["applyComp"], "needs": "approved_by"})


def declineApproval(guest_id: str, reason: str, declined_by: str = "") -> dict:
    """Close a pending compensation request without spending anything."""
    with db() as conn:
        guest = conn.execute("SELECT name FROM guests WHERE guest_id = ?", (guest_id,)).fetchone()
        if guest is None:
            return fail("NOT_FOUND", f"I do not have a guest {guest_id}.")

        pending = _rows(conn.execute(
            "SELECT id, amount_inr FROM approvals WHERE guest_id = ? AND status = 'pending'",
            (guest_id,)))
        if not pending:
            return ok(f"Nothing is waiting for approval for {guest['name']}.",
                      {"closed": 0})

        total = sum(p["amount_inr"] for p in pending)
        conn.execute(
            "UPDATE approvals SET status = 'declined', decided_by = ?, decided_at = ? "
            "WHERE guest_id = ? AND status = 'pending'",
            (declined_by or "Duty Manager (voice)", now_iso(), guest_id))
        _audit(conn, "comp_declined", "guest", guest_id,
               {"amount_inr": total, "reason": reason,
                "decided_by": declined_by or "Duty Manager (voice)"})

        return ok(f"Declined. Nothing recorded against {guest['name']}.",
                  {"closed": len(pending), "declined_total_inr": total})


def createDepartmentTask(department: str, location: str, priority: str,
                           description: str) -> dict:
    department = str(department).strip().lower()
    priority = str(priority).strip().lower()
    if priority not in SLA_MINUTES:
        priority = "medium"
    sla_minutes = SLA_MINUTES[priority]
    due = (clock_now() + timedelta(minutes=sla_minutes)).strftime("%Y-%m-%dT%H:%M:%S")

    with db() as conn:
        existing = conn.execute(
            "SELECT task_id, sla_due FROM tasks WHERE department = ? AND location = ? "
            "AND lower(description) = lower(?) AND status IN ('open','in_progress') "
            "ORDER BY task_id LIMIT 1",
            (department, location, description)).fetchone()
        if existing is not None:
            return ok(
                f"There is already an open {department} task for {location}: "
                f"{description}. I did not raise a duplicate.",
                {"task_id": existing["task_id"], "duplicate": True,
                 "sla_due": existing["sla_due"]})

        cur = conn.execute(
            "INSERT INTO tasks (created_at, department, location, priority, description, sla_due) "
            "VALUES (?,?,?,?,?,?)",
            (now_iso(), department, location, priority, description, due))
        _audit(conn, "task_created", "task", str(cur.lastrowid),
               {"department": department, "location": location,
                "priority": priority, "description": description})
        return ok(f"{department.title()} task raised for {location}, due in {sla_minutes} minutes.",
                  {"task_id": cur.lastrowid, "sla_due": due, "sla_minutes": sla_minutes})


def findTask(query: str = "") -> dict:
    """Resolve a spoken description to a task id before changing anything."""
    needle = str(query or "").strip()
    with db() as conn:
        sql = ("SELECT task_id, department, location, priority, description, status, "
               "sla_due, assignee FROM tasks WHERE status IN ('open','in_progress')")
        params: list = []
        if needle:
            like = f"%{needle.lower()}%"
            sql += (" AND (lower(description) LIKE ? OR lower(location) LIKE ? "
                    "OR lower(department) LIKE ?)")
            params = [like, like, like]
        rows = _rows(conn.execute(sql + " ORDER BY sla_due", params))

    if not rows:
        return fail("NOT_FOUND", f"No open task matches '{query}'.",
                    {"open_tasks": 0},
                    hints={"next": ["getShiftSummary", "createDepartmentTask"]})

    if len(rows) == 1:
        task = rows[0]
        return ok(
            f"Found one: {task['description']} at {task['location']} "
            f"({task['department']}, {task['priority']}). Task {task['task_id']}.",
            {"tasks": rows, "task_id": task["task_id"]})

    listed = "; ".join(f"{t['task_id']}: {t['description']}" for t in rows[:4])
    return ok(f"{len(rows)} open tasks match: {listed}.",
              {"tasks": rows[:5], "count": len(rows)})


def updateTask(task_id: int, status: str, note: str | None = None) -> dict:
    status = str(status).strip().lower()
    if status not in TASK_STATUSES:
        return fail("ERROR", f"{status} is not a task status.",
                    hints={"valid": sorted(TASK_STATUSES)})
    with db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (int(task_id),)).fetchone()
        if task is None:
            return fail("NOT_FOUND", f"No task number {task_id}.",
                        hints={"next": ["findTask", "getShiftSummary"]})
        closed = now_iso() if status in ("done", "cancelled") else None
        conn.execute("UPDATE tasks SET status = ?, note = COALESCE(?, note), closed_at = ? "
                     "WHERE task_id = ?", (status, note, closed, int(task_id)))
        _audit(conn, "task_updated", "task", str(task_id),
               {"from": task["status"], "to": status, "note": note,
                "description": task["description"]})
        return ok(
            f"Marked '{task['description']}' at {task['location']} as "
            f"{status.replace('_', ' ')}.",
            {"task_id": int(task_id), "status": status,
             "description": task["description"], "location": task["location"]})


def getAvailabilitySummary() -> dict:
    """How many rooms are free, by category, plus anything out of service."""
    with db() as conn:
        rooms = _rows(conn.execute(
            "SELECT room_no, floor, category, status FROM rooms ORDER BY room_no"))

    total = len(rooms)
    vacant = [r for r in rooms if r["status"] == "vacant"]
    occupied = [r for r in rooms if r["status"] == "occupied"]
    oos = [r["room_no"] for r in rooms if r["status"] == "out_of_service"]

    by_category: dict[str, list[str]] = {}
    for room in vacant:
        by_category.setdefault(room["category"], []).append(room["room_no"])

    parts = [f"{len(vacant)} of {total} rooms are free right now"]
    if by_category:
        parts.append(", ".join(f"{len(v)} {k}" for k, v in by_category.items()))
    if oos:
        parts.append("out of service: " + ", ".join(oos))
    message = "; ".join(parts) + "."

    return ok(message, {
        "total_rooms": total,
        "vacant": len(vacant),
        "occupied": len(occupied),
        "out_of_service": oos,
        "vacant_by_category": by_category,
        "vacant_rooms": [r["room_no"] for r in vacant],
    })


def getPolicy(topic: str) -> dict:
    words = {w for w in re.findall(r"[a-z0-9]+", str(topic).lower()) if len(w) > 3}
    with db() as conn:
        chunks = _rows(conn.execute("SELECT topic, body FROM policy_chunks"))
    scored = []
    for c in chunks:
        body = (c["topic"] + " " + c["body"]).lower()
        score = sum(1 for w in words if w in body)
        if score:
            scored.append((score, c))
    if not scored:
        return ok("I have no policy section on that.", {"sections": []})
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [c for _, c in scored[:2]]
    return ok("Here is what the policy says.", {"sections": top})


def scheduleFollowup(room_no: str, minutes: int, note: str) -> dict:
    try:
        mins = int(minutes)
    except (TypeError, ValueError):
        return fail("ERROR", "I need the reminder as a number of minutes.")
    due = (clock_now() + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S")
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO followups (created_at, room_no, due_at, note) VALUES (?,?,?,?)",
            (now_iso(), room_no, due, note))
        _audit(conn, "followup_scheduled", "room", room_no,
               {"minutes": mins, "note": note, "due_at": due})
        return ok(f"I will remind you about {room_no} in {mins} minutes.",
                  {"followup_id": cur.lastrowid, "due_at": due})


def findPartnerHotel(category: str, max_distance_km: float = 0) -> dict:
    """Partner properties that can take a guest tonight, nearest first."""
    category = str(category).strip().lower()
    if category not in CATEGORY_RANK:
        return fail("ERROR", f"I do not have a room category called {category}.",
                    hints={"valid": list(CATEGORY_RANK)})

    with db() as conn:
        rows = _rows(conn.execute(
            "SELECT partner_id, name, area, distance_km, category, rate_inr, rooms_available, phone "
            "FROM partner_hotels WHERE active = 1 AND rooms_available > 0"))

    wanted = CATEGORY_RANK[category]
    options = [r for r in rows if CATEGORY_RANK[r["category"]] >= wanted]
    if max_distance_km:
        try:
            limit = float(max_distance_km)
        except (TypeError, ValueError):
            limit = 0
        if limit:
            options = [r for r in options if r["distance_km"] <= limit]
    options.sort(key=lambda r: (CATEGORY_RANK[r["category"]] - wanted, r["distance_km"]))

    if not options:
        return fail("UNAVAILABLE",
                    "No partner property has a room of that standard tonight.",
                    {"partners_checked": len(rows)},
                    hints={"next": ["findPartnerHotel"]})

    first = options[0]
    upgrade = CATEGORY_RANK[first["category"]] > wanted
    return ok(
        f"{first['name']} in {first['area']} is {first['distance_km']} km away, "
        f"{first['category']}, at {first['rate_inr']} rupees."
        + (" That is an upgrade on what they booked." if upgrade else ""),
        {"options": options[:3]},
    )


def arrangeTransport(guest_id: str, partner_id: str, minutes: int = 15) -> dict:
    """Book the car that takes the guest to the partner property."""
    with db() as conn:
        guest = conn.execute("SELECT name FROM guests WHERE guest_id = ?", (guest_id,)).fetchone()
        partner = conn.execute(
            "SELECT name, distance_km FROM partner_hotels WHERE partner_id = ?",
            (partner_id,)).fetchone()
        if guest is None:
            return fail("NOT_FOUND", f"I do not have a guest {guest_id}.")
        if partner is None:
            return fail("NOT_FOUND", f"I do not have a partner property {partner_id}.")

        try:
            mins = max(5, int(minutes))
        except (TypeError, ValueError):
            mins = 15

        pickup = (clock_now() + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S")
        fare = int(200 + partner["distance_km"] * 180)
        cur = conn.execute(
            "INSERT INTO transport_requests (created_at, guest_id, partner_id, pickup_at, fare_inr) "
            "VALUES (?,?,?,?,?)",
            (now_iso(), guest_id, partner_id, pickup, fare))
        _audit(conn, "transport_booked", "guest", guest_id,
               {"partner": partner["name"], "pickup_at": pickup, "fare_inr": fare})
        return ok(f"Car booked for {guest['name']} in {mins} minutes, {fare} rupees.",
                  {"transport_id": cur.lastrowid, "pickup_at": pickup, "fare_inr": fare,
                   "partner": partner["name"]})


def walkGuest(guest_id: str, partner_id: str, transport_id: int, reason: str,
              approved_by: str = "") -> dict:
    """Relocate a guest to a partner property. General Manager only."""
    with db() as conn:
        guest = conn.execute("SELECT name, tier FROM guests WHERE guest_id = ?",
                             (guest_id,)).fetchone()
        partner = conn.execute("SELECT name, rate_inr FROM partner_hotels WHERE partner_id = ?",
                               (partner_id,)).fetchone()
        transport = conn.execute(
            "SELECT transport_id, fare_inr FROM transport_requests WHERE transport_id = ?",
            (int(transport_id) if str(transport_id).isdigit() else -1,)).fetchone()
        if guest is None:
            return fail("NOT_FOUND", f"I do not have a guest {guest_id}.")
        if partner is None:
            return fail("NOT_FOUND", f"I do not have a partner property {partner_id}.")
        if transport is None:
            return fail("NOT_FOUND",
                        "There is no car booked for this guest yet.",
                        hints={"next": ["arrangeTransport"]})

        res = conn.execute(
            "SELECT res_id, room_no FROM reservations WHERE guest_id = ? "
            "AND status IN ('expected','in_house') ORDER BY arrival_date LIMIT 1",
            (guest_id,)).fetchone()
        if res is None:
            return fail("CONFLICT", "Nobody by that name is due to arrive or in house.")

        cost = partner["rate_inr"] + transport["fare_inr"]

        if not approved_by:
            return fail(
                "APPROVAL_REQUIRED",
                f"Walking {guest['name']} to {partner['name']} costs about {cost} rupees. "
                "Only the General Manager can authorise that.",
                {"estimated_cost_inr": cost, "required_role": "gm",
                 "partner": partner["name"]},
                hints={"next": ["walkGuest"], "needs": "the General Manager's name"})

        staff = conn.execute(
            "SELECT name, role FROM staff WHERE lower(name) = lower(?) AND active = 1",
            (approved_by,)).fetchone()
        if staff is None or staff["role"] != "gm":
            who = staff["name"] if staff else approved_by
            return fail("APPROVAL_REQUIRED",
                        f"{who} cannot authorise a walk. That is the General Manager's call.",
                        {"required_role": "gm", "estimated_cost_inr": cost})

        conn.execute("UPDATE reservations SET status = 'walked' WHERE res_id = ?",
                     (res["res_id"],))
        if res["room_no"]:
            conn.execute(
                "UPDATE rooms SET status = 'vacant', housekeeping = 'dirty' WHERE room_no = ?",
                (res["room_no"],))
        cur = conn.execute(
            "INSERT INTO walks (created_at, guest_id, partner_id, transport_id, reason, "
            "approved_by, cost_inr) VALUES (?,?,?,?,?,?,?)",
            (now_iso(), guest_id, partner_id, transport["transport_id"], reason,
             staff["name"], cost))
        _audit(conn, "guest_walked", "guest", guest_id,
               {"partner": partner["name"], "reason": reason,
                "approved_by": staff["name"], "cost_inr": cost})

        return ok(
            f"{guest['name']} is walked to {partner['name']}. Car confirmed, "
            f"{cost} rupees on our account.",
            {"walk_id": cur.lastrowid, "partner": partner["name"],
             "cost_inr": cost, "approved_by": staff["name"]})


def getFolio(guest_id: str) -> dict:
    """The guest's bill: every posted line, what has been reversed, and the balance."""
    with db() as conn:
        res = conn.execute(
            "SELECT res_id, room_no, status FROM reservations WHERE guest_id = ? "
            "AND status IN ('in_house','expected','checked_out') "
            "ORDER BY arrival_date DESC LIMIT 1", (guest_id,)).fetchone()
        if res is None:
            return fail("NOT_FOUND", f"No reservation on file for {guest_id}.")
        guest = conn.execute("SELECT name FROM guests WHERE guest_id = ?",
                             (guest_id,)).fetchone()
        charges = _rows(conn.execute(
            "SELECT charge_id, category, description, amount_inr, posted_at, status, "
            "reversed_reason FROM folio_charges WHERE res_id = ? ORDER BY charge_id",
            (res["res_id"],)))

    posted = [c for c in charges if c["status"] == "posted"]
    reversed_lines = [c for c in charges if c["status"] == "reversed"]
    balance = sum(c["amount_inr"] for c in posted)

    parts = [f"{guest['name']}'s folio has {len(posted)} open "
             f"charge{'s' if len(posted) != 1 else ''} totalling {balance} rupees"]
    if reversed_lines:
        parts.append(f"{len(reversed_lines)} already reversed")
    return ok("; ".join(parts) + ".",
              {"res_id": res["res_id"], "room_no": res["room_no"],
               "charges": charges, "balance_inr": balance,
               "reversed_inr": sum(c["amount_inr"] for c in reversed_lines)})


def reverseCharge(charge_id: int, reason: str, approved_by: str = "") -> dict:
    """Correct a posted charge. This fixes the record; it is not a goodwill comp."""
    try:
        cid = int(charge_id)
    except (TypeError, ValueError):
        return fail("ERROR", "I need the charge number, not a description.")

    with db() as conn:
        charge = conn.execute("SELECT * FROM folio_charges WHERE charge_id = ?",
                              (cid,)).fetchone()
        if charge is None:
            return fail("NOT_FOUND", f"No charge number {cid} on file.")
        if charge["status"] == "reversed":
            return fail("CONFLICT",
                        f"Charge {cid} has already been reversed, so nothing more to correct.",
                        {"charge_id": cid})

    decision = policy.evaluate("reversal", {
        "amount_inr": charge["amount_inr"],
        "approved_by": approved_by or None,
    })

    if decision["decision"] == "REQUIRE_APPROVAL":
        return fail("APPROVAL_REQUIRED", decision["message"],
                    {"charge_id": cid, "amount_inr": charge["amount_inr"],
                     "limit_inr": decision["limit_inr"],
                     "required_role": decision["required_role"],
                     "rule_code": decision["rule_code"]},
                    hints={"next": ["reverseCharge"], "needs": "approved_by"})

    approver = None
    if decision["decision"] == "ALLOW_APPROVED":
        approver = decision.get("approved_by") or None

    with db() as conn:
        conn.execute(
            "UPDATE folio_charges SET status = 'reversed', reversed_reason = ? "
            "WHERE charge_id = ? AND status = 'posted'",
            (reason, cid))
        _audit(conn, "charge_reversed", "charge", str(cid),
               {"description": charge["description"], "amount_inr": charge["amount_inr"],
                "reason": reason, "approved_by": approver})
        return ok(
            f"Reversed {charge['description']} at {charge['amount_inr']} rupees. "
            "That was a correction, not a goodwill comp.",
            {"charge_id": cid, "amount_inr": charge["amount_inr"],
             "approved_by": approver, "reason": reason})


def at_risk_tasks(threshold_minutes: int = 15) -> list[dict]:
    """Open tasks at or past their SLA threshold. Used by the alert loop, not a tool."""
    with db() as conn:
        rows = _rows(conn.execute(
            "SELECT task_id, department, location, priority, description, sla_due, assignee "
            "FROM tasks WHERE status IN ('open','in_progress')"))
    flagged = []
    for row in rows:
        minutes = _minutes_until(row["sla_due"])
        if minutes <= threshold_minutes:
            row["minutes_left"] = minutes
            flagged.append(row)
    flagged.sort(key=lambda r: r["minutes_left"])
    return flagged


ROLE_LABEL = {
    "front_desk": "front desk",
    "duty_manager": "duty manager",
    "gm": "general manager",
    "maintenance_lead": "maintenance lead",
    "housekeeping_lead": "housekeeping lead",
    "security_lead": "security lead",
}


def _join_names(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + " and " + names[-1]


def getRoster(query: str = "", min_authority_inr: int = 0) -> dict:
    """Who does what: role, department, approval authority, and whether they are on duty."""
    needle = str(query or "").strip().lower()
    try:
        minimum = int(min_authority_inr or 0)
    except (TypeError, ValueError):
        minimum = 0

    with db() as conn:
        rows = _rows(conn.execute(
            "SELECT name, role, department, authority_inr, phone, on_duty "
            "FROM staff WHERE active = 1 ORDER BY authority_inr DESC, name"))

    people = []
    for row in rows:
        label = ROLE_LABEL.get(row["role"], row["role"])
        haystack = " ".join(str(v or "") for v in
                            (row["name"], row["role"], row["department"], label)).lower()
        if needle and needle not in haystack:
            continue
        if minimum and row["authority_inr"] < minimum:
            continue
        people.append({**row, "on_duty": bool(row["on_duty"])})

    if not people:
        return fail("NOT_FOUND", f"Nobody on the roster matches '{query}'.",
                    {"roster_size": len(rows)})

    if minimum:
        names = [p["name"] for p in people[:4]]
        message = f"{_join_names(names)} can approve {minimum} rupees."
    elif len(people) == 1:
        person = people[0]
        message = (f"{person['name']} is the "
                   f"{ROLE_LABEL.get(person['role'], person['role'])}, "
                   f"{'on duty' if person['on_duty'] else 'off duty'}"
                   + (f", on {person['phone']}" if person["phone"] else "") + ".")
    else:
        message = (f"{len(people)} people: "
                   f"{_join_names([p['name'] for p in people])}.")

    return ok(message, {"people": people, "count": len(people)})


REGISTRY = {
    "getShiftSummary": getShiftSummary,
    "getAvailabilitySummary": getAvailabilitySummary,
    "getRoster": getRoster,
    "getRoom": getRoom,
    "findAvailableRooms": findAvailableRooms,
    "setRoomStatus": setRoomStatus,
    "getGuestContext": getGuestContext,
    "searchGuest": searchGuest,
    "getArrivals": getArrivals,
    "moveRoom": moveRoom,
    "extendStay": extendStay,
    "applyComp": applyComp,
    "declineApproval": declineApproval,
    "createDepartmentTask": createDepartmentTask,
    "updateTask": updateTask,
    "findTask": findTask,
    "getPolicy": getPolicy,
    "scheduleFollowup": scheduleFollowup,
    "findPartnerHotel": findPartnerHotel,
    "arrangeTransport": arrangeTransport,
    "walkGuest": walkGuest,
    "getFolio": getFolio,
    "reverseCharge": reverseCharge,
}
