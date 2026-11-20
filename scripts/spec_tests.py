"""Phase P1 acceptance tests.

    .venv\\Scripts\\python.exe -m scripts.spec_tests

Covers the three things the spec says must never regress: policy decisions,
error semantics, and idempotency of money writes.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

from app import db, policy, tools

FAILS: list[str] = []


def check(name: str, condition: bool, got=None) -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f"  -> {got}" if got is not None else ""))
        FAILS.append(name)


def section(title: str) -> None:
    print(f"\n== {title}")


def main() -> None:
    db.init(reset=True)

    # --- policy engine ------------------------------------------------
    section("policy engine")

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 1000,
                                 "local_time": datetime(2026, 11, 20, 15, 0)})
    check("gold 1000 at 15:00 -> ALLOW COMP-GOLD",
          d["decision"] == "ALLOW" and d["rule_code"] == "COMP-GOLD", d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 3000,
                                 "local_time": datetime(2026, 11, 20, 15, 0)})
    check("gold 3000 at 15:00 -> REQUIRE_APPROVAL",
          d["decision"] == "REQUIRE_APPROVAL" and d["limit_inr"] == 1500, d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 2500,
                                 "local_time": datetime(2026, 11, 20, 23, 40)})
    check("gold 2500 at 23:40 -> ALLOW COMP-LATE-GOLD (time-aware)",
          d["decision"] == "ALLOW" and d["rule_code"] == "COMP-LATE-GOLD", d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 2500,
                                 "local_time": datetime(2026, 11, 20, 15, 0)})
    check("gold 2500 at 15:00 -> REQUIRE_APPROVAL (same amount, different hour)",
          d["decision"] == "REQUIRE_APPROVAL", d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 6000,
                                 "local_time": datetime(2026, 11, 20, 23, 40)})
    check("gold 6000 -> ESCALATE to GM",
          d["decision"] == "ESCALATE" and d["escalate_to"] == "gm", d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 3000,
                                 "local_time": datetime(2026, 11, 20, 15, 0),
                                 "approved_by": "Ravi"})
    check("gold 3000 approved by duty manager -> ALLOW_APPROVED",
          d["decision"] == "ALLOW_APPROVED" and d["approved_role"] == "duty_manager", d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 3000,
                                 "local_time": datetime(2026, 11, 20, 15, 0),
                                 "approved_by": "Kiran"})
    check("gold 3000 approved by front desk -> still REQUIRE_APPROVAL",
          d["decision"] == "REQUIRE_APPROVAL", d)

    # --- W1 ----------------------------------------------------------
    section("W1 room defect -> move -> comp")

    ctx = tools.getGuestContext("408")
    check("guest context resolves", ctx["ok"] and ctx["data"]["guest"]["name"] == "Mr. Arjun Rao", ctx)
    check("ceiling exposed to agent", ctx["data"]["guest"]["comp_ceiling_inr"] in (1500, 2500), ctx)

    bad = tools.moveRoom("G-1001", "408", "508", "test")
    check("moving into an occupied room -> UNAVAILABLE with alternatives",
          bad["status"] == "UNAVAILABLE" and bad["data"]["available_now"], bad)

    avail = tools.findAvailableRooms("deluxe", 4)
    check("floor 4 has no deluxe; 604 offered",
          avail["data"]["matches_on_requested_floor"] == []
          and avail["data"]["same_category_elsewhere"][0]["room_no"] == "604", avail)

    moved = tools.moveRoom("G-1001", "408", "604", "AC rattling, guest waited 60 minutes",
                            request_id="mv-1")
    check("move succeeds", moved["ok"] and moved["data"]["to_room"] == "604", moved)

    cleared = tools.findAvailableRooms("deluxe", 0)
    check("a category with no vacancies returns upgrades, not an error",
          cleared["ok"] and cleared["data"]["upgrade_options"], cleared)

    summary = tools.getAvailabilitySummary()
    check("availability summary counts every status",
          summary["ok"]
          and summary["data"]["vacant"] == len(summary["data"]["vacant_rooms"])
          and summary["data"]["occupied"] + summary["data"]["vacant"]
          + len(summary["data"]["out_of_service"]) == summary["data"]["total_rooms"],
          summary)

    room605 = tools.getRoom("605")
    check("a room reports its open task in the spoken message",
          room605["ok"] and "shower drain" in room605["message"].lower()
          and room605["data"]["tasks"], room605)

    again = tools.moveRoom("G-1001", "408", "604", "retry", request_id="mv-1")
    check("move is idempotent on request_id", again["ok"], again)

    blocked = tools.applyComp("G-1001", 3000, "room defect and 60 minute wait")
    check("3000 comp without approval -> APPROVAL_REQUIRED",
          blocked["status"] == "APPROVAL_REQUIRED"
          and blocked["data"]["limit_inr"] in (1500, 2500), blocked)

    approved = tools.applyComp("G-1001", 3000, "room defect and 60 minute wait",
                                approved_by="Ravi", request_id="comp-1")
    check("approved by duty manager -> DONE with approver recorded",
          approved["ok"] and approved["data"]["approved_by"] == "Ravi", approved)

    dup = tools.applyComp("G-1001", 3000, "retry", approved_by="Ravi", request_id="comp-1")
    check("comp is idempotent on request_id", dup["ok"], dup)

    # --- approval closure ---------------------------------------------
    section("approval closure")

    def pending_count() -> int:
        c = db.connect()
        try:
            return c.execute(
                "SELECT COUNT(*) c FROM approvals WHERE status = 'pending'").fetchone()["c"]
        finally:
            c.close()

    tools.applyComp("G-1001", 4000, "request the manager will decline")
    check("a blocked comp creates a pending request", pending_count() >= 1, pending_count())

    declined = tools.declineApproval("G-1001", "manager refused the amount", "")
    check("decline closes the pending request",
          declined["ok"] and pending_count() == 0, declined)

    tools.applyComp("G-1001", 4000, "request the manager will reduce")
    reduced = tools.applyComp("G-1001", 2500, "manager authorised 2500 only",
                              approved_by="Ravi")
    check("approving a LOWER amount also closes the original request",
          reduced["ok"] and pending_count() == 0, reduced)

    within = tools.applyComp("G-1001", 500, "small goodwill, well inside the ceiling",
                             approved_by="Ravi")
    check("an in-policy comp records NO approver, even if one is passed",
          within["ok"] and not within["data"].get("approved_by"), within)

    task = tools.createDepartmentTask("maintenance", "408", "high", "Inspect AC unit")
    check("task created with SLA", task["ok"] and task["data"]["sla_minutes"] == 30, task)

    conflict = tools.setRoomStatus("408", "vacant", "put back on sale")
    check("room with open defect cannot go vacant -> CONFLICT",
          conflict["status"] == "CONFLICT" and conflict["data"]["open_task_ids"], conflict)

    tools.updateTask(task["data"]["task_id"], "done", "AC repaired")
    released = tools.setRoomStatus("408", "vacant", "repair complete")
    check("room released after task closed", released["ok"], released)

    fup = tools.scheduleFollowup("408", 30, "Confirm guest settled in 604")
    check("follow-up scheduled", fup["ok"], fup)

    # --- W2 ----------------------------------------------------------
    section("W2 late checkout vs arrival (capped at ETA)")

    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    arrivals = tools.getArrivals("508", tomorrow)
    check("508 has a next-day arrival at 14:00",
          arrivals["ok"] and arrivals["data"]["arrivals"][0]["arrival_eta"] == "14:00", arrivals)

    late = tools.extendStay("G-1002", "2026-12-24")
    check("extension on a date with no arrival -> DONE",
          late["ok"] and late["data"]["new_departure"].startswith("2026-12-24"), late)
    tools.extendStay("G-1002", tomorrow, rate_inr=6500)  # reset departure for the clash case

    clash = tools.extendStay("G-1002", f"{tomorrow}T16:00")
    check("extension past the arrival ETA -> CONFLICT with latest_possible_checkout",
          clash["status"] == "CONFLICT"
          and clash["data"]["latest_possible_checkout"].endswith("T14:00"), clash)

    capped = tools.extendStay("G-1002", f"{tomorrow}T13:30")
    check("extension before the ETA -> DONE", capped["ok"], capped)

    # --- W3 ----------------------------------------------------------
    section("W3 walk a guest")

    partners = tools.findPartnerHotel("deluxe", 0)
    check("partner options found for a deluxe guest",
          partners["ok"] and partners["data"]["options"], partners)

    near = tools.findPartnerHotel("deluxe", 2.0)
    check("distance filter is applied",
          near["ok"] and all(p["distance_km"] <= 2.0 for p in near["data"]["options"]), near)

    transport = tools.arrangeTransport("G-2003", "P-01", 15)
    check("transport booked", transport["ok"] and transport["data"]["transport_id"], transport)
    tid = transport["data"]["transport_id"]

    blocked = tools.walkGuest("G-2003", "P-01", tid, "hotel oversold", "")
    check("walk without approval -> APPROVAL_REQUIRED for the GM",
          blocked["status"] == "APPROVAL_REQUIRED"
          and blocked["data"]["required_role"] == "gm", blocked)

    wrong = tools.walkGuest("G-2003", "P-01", tid, "hotel oversold", "Ravi")
    check("a duty manager cannot authorise a walk",
          wrong["status"] == "APPROVAL_REQUIRED", wrong)

    done = tools.walkGuest("G-2003", "P-01", tid, "hotel oversold", "Arun")
    check("GM approval completes the walk", done["ok"], done)

    twice = tools.walkGuest("G-2003", "P-01", tid, "hotel oversold", "Arun")
    check("the same guest cannot be walked twice",
          twice["status"] == "CONFLICT", twice)

    # --- W4 ----------------------------------------------------------
    section("W4 billing dispute")

    folio = tools.getFolio("G-1001")
    check("folio returns posted line items and a balance",
          folio["ok"] and len(folio["data"]["charges"]) >= 2
          and folio["data"]["balance_inr"] > 0, folio)

    charges = folio["data"]["charges"]
    minibar = next(c for c in charges if "inibar" in c["description"])
    room_charge = next(c for c in charges if "Room charge" in c["description"])

    small = tools.reverseCharge(minibar["charge_id"], "guest never touched it", "")
    check("a small reversal needs no approver",
          small["ok"] and small["data"]["approved_by"] is None, small)

    twice = tools.reverseCharge(minibar["charge_id"], "again", "")
    check("the same charge cannot be reversed twice",
          twice["status"] == "CONFLICT", twice)

    big = tools.reverseCharge(room_charge["charge_id"], "rate was quoted lower", "")
    check("a large reversal -> APPROVAL_REQUIRED",
          big["status"] == "APPROVAL_REQUIRED" and big["data"]["required_role"], big)

    weak = tools.reverseCharge(room_charge["charge_id"], "rate was quoted lower", "Ravi")
    check("approval below the amount is refused",
          weak["status"] == "APPROVAL_REQUIRED", weak)

    done = tools.reverseCharge(room_charge["charge_id"], "rate was quoted lower", "Arun")
    check("GM approval completes the reversal",
          done["ok"] and done["data"]["approved_by"] == "Arun", done)

    after = tools.getFolio("G-1001")
    check("reversed lines leave the balance",
          after["ok"] and after["data"]["balance_inr"] == 0, after)

    # --- W5 ----------------------------------------------------------
    section("W5 shift handover")

    summary = tools.getShiftSummary()
    check("handover reports the move and the comp",
          summary["ok"] and summary["data"]["moves"] and summary["data"]["comps"], summary)
    check("handover reports open work", "follow" in summary["message"].lower()
          or summary["data"]["followups"] or summary["data"]["tasks"], summary["message"])
    check("handover message is speakable (no JSON, no field names)",
          "{" not in summary["message"] and "_inr" not in summary["message"], summary["message"])

    # --- shift clock ---------------------------------------------------
    section("shift clock")

    os.environ["PO_SIM_HOUR"] = "23"
    night = tools.getGuestContext("604")
    check("simulated 23:00 raises the Gold ceiling to 2500",
          night["data"]["guest"]["comp_ceiling_inr"] == 2500
          and night["data"]["guest"]["ceiling_rule"] == "COMP-LATE-GOLD",
          night["data"]["guest"])
    os.environ.pop("PO_SIM_HOUR", None)

    # --- proactive SLA alert ------------------------------------------
    section("proactive SLA alert")

    db.init(reset=True)
    from app.main import _raise_alerts  # noqa: E402 - imported late to avoid side effects

    first, fresh = _raise_alerts()
    check("a task close to breaching is flagged",
          any(t["location"] == "605" for t in first), first)
    check("the newly flagged task is reported as fresh",
          any(t["location"] == "605" for t in fresh), fresh)

    second, second_fresh = _raise_alerts()
    check("re-polling reports nothing as fresh", second_fresh == [], second_fresh)
    conn = db.connect()
    try:
        logged = conn.execute(
            "SELECT COUNT(*) c FROM audit WHERE action = 'sla_alert'").fetchone()["c"]
    finally:
        conn.close()
    check("the alert is logged once, not on every poll",
          logged == len(first), f"logged={logged} first={len(first)}")

    flagged_id = next(t["task_id"] for t in first if t["location"] == "605")
    tools.updateTask(flagged_id, "done", "drain cleared")
    check("closing the task clears the alert",
          not any(t["task_id"] == flagged_id for t in tools.at_risk_tasks(15)),
          tools.at_risk_tasks(15))

    # --- server-initiated interaction ---------------------------------
    section("server-initiated interaction")

    from app import agora_agent as ag  # noqa: E402

    saved_agent = ag.service.current_agent_id
    ag.service.current_agent_id = None
    try:
        asyncio.run(ag.service.speak("test"))
        check("speak refuses without a live session", False)
    except RuntimeError:
        check("speak refuses without a live session", True)

    try:
        asyncio.run(ag.service.think("test"))
        check("think refuses without a live session", False)
    except RuntimeError:
        check("think refuses without a live session", True)
    ag.service.current_agent_id = saved_agent

    # --- roster -------------------------------------------------------
    section("roster")

    everyone = tools.getRoster("", 0)
    check("the roster lists all six people",
          everyone["ok"] and everyone["data"]["count"] == 6, everyone)

    maintenance = tools.getRoster("maintenance", 0)
    check("a department query finds its lead",
          maintenance["ok"] and maintenance["data"]["people"][0]["name"] == "Suresh",
          maintenance)

    approvers = tools.getRoster("", 4000)
    names = [p["name"] for p in approvers["data"]["people"]] if approvers["ok"] else []
    check("an authority filter returns only people who can approve it",
          approvers["ok"] and "Arun" in names and "Ravi" in names
          and "Suresh" not in names, approvers)

    missing = tools.getRoster("nobody", 0)
    check("an unknown name returns NOT_FOUND", missing["status"] == "NOT_FOUND", missing)

    # --- unknown rooms and duplicate tasks -----------------------------
    section("bad room numbers and duplicate tasks")

    wrong_room = tools.getGuestContext("498")
    check("an unknown room explains the valid range and says not to retry",
          wrong_room["status"] == "NOT_FOUND"
          and "401 to 408" in wrong_room["message"]
          and wrong_room["hints"].get("next"), wrong_room)

    task_one = tools.createDepartmentTask("maintenance", "408", "emergency",
                                          "Broken air conditioning in room 408")
    task_two = tools.createDepartmentTask("maintenance", "408", "emergency",
                                          "Broken air conditioning in room 408")
    check("an identical open task is not raised twice",
          task_two["ok"] and task_two["data"].get("duplicate") is True
          and task_two["data"]["task_id"] == task_one["data"]["task_id"],
          task_two)

    found = tools.findTask("AC filter")
    check("a spoken description resolves to the right task",
          found["ok"] and "filter" in found["data"]["tasks"][0]["description"].lower(),
          found)

    closed = tools.updateTask(found["data"]["task_id"], "done", "fixed by Suresh")
    check("closing a task names it in the spoken confirmation",
          closed["ok"] and "filter" in closed["message"].lower(), closed)

    # --- seed coherence -----------------------------------------------
    section("seed coherence")

    db.init(reset=True)
    conn = db.connect()
    try:
        orphans = conn.execute(
            "SELECT COUNT(*) n FROM rooms r WHERE r.status = 'occupied' AND NOT EXISTS ("
            "SELECT 1 FROM reservations v WHERE v.room_no = r.room_no AND v.status = 'in_house')"
        ).fetchone()["n"]
        occupied = conn.execute(
            "SELECT COUNT(*) n FROM rooms WHERE status = 'occupied'").fetchone()["n"]
        in_house = conn.execute(
            "SELECT COUNT(*) n FROM reservations WHERE status = 'in_house'").fetchone()["n"]
    finally:
        conn.close()
    check("every occupied room has an in-house guest", orphans == 0, orphans)
    check("in-house reservations match occupied rooms", in_house == occupied,
          f"occupied={occupied} in_house={in_house}")

    # --- envelopes ---------------------------------------------------
    section("envelope contract")

    for name, fn in tools.REGISTRY.items():
        r = fn() if name == "getShiftSummary" else None
        if r is None:
            continue
        check(f"{name} returns the full envelope",
              set(r) == {"ok", "status", "message", "data", "hints"}, r)

    missing = tools.getRoom("9999")
    check("unknown room -> NOT_FOUND with a next step",
          missing["status"] == "NOT_FOUND" and missing["hints"].get("next"), missing)

    print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
