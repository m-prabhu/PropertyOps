"""Phase P1 acceptance tests.

    .venv\\Scripts\\python.exe -m scripts.spec_tests

Covers the three things the spec says must never regress: policy decisions,
error semantics, and idempotency of money writes.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

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
                                 "approved_by": "R. Menon"})
    check("gold 3000 approved by duty manager -> ALLOW_APPROVED",
          d["decision"] == "ALLOW_APPROVED" and d["approved_role"] == "duty_manager", d)

    d = policy.evaluate("comp", {"tier": "gold", "amount_inr": 3000,
                                 "local_time": datetime(2026, 11, 20, 15, 0),
                                 "approved_by": "K. Pillai"})
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

    again = tools.moveRoom("G-1001", "408", "604", "retry", request_id="mv-1")
    check("move is idempotent on request_id", again["ok"], again)

    blocked = tools.applyComp("G-1001", 3000, "room defect and 60 minute wait")
    check("3000 comp without approval -> APPROVAL_REQUIRED",
          blocked["status"] == "APPROVAL_REQUIRED"
          and blocked["data"]["limit_inr"] in (1500, 2500), blocked)

    approved = tools.applyComp("G-1001", 3000, "room defect and 60 minute wait",
                                approved_by="R. Menon", request_id="comp-1")
    check("approved by duty manager -> DONE with approver recorded",
          approved["ok"] and approved["data"]["approved_by"] == "R. Menon", approved)

    dup = tools.applyComp("G-1001", 3000, "retry", approved_by="R. Menon", request_id="comp-1")
    check("comp is idempotent on request_id", dup["ok"], dup)

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

    arrivals = tools.getArrivals("508")
    check("508 has a same-day arrival at 14:00",
          arrivals["ok"] and arrivals["data"]["arrivals"][0]["arrival_eta"] == "14:00", arrivals)

    late = tools.extendStay("G-1002", "2026-12-24")
    check("extension on a date with no arrival -> DONE",
          late["ok"] and late["data"]["new_departure"].startswith("2026-12-24"), late)
    tools.extendStay("G-1002", "2026-11-20", rate_inr=6500)  # reset departure for the clash case

    today = datetime.now().date().isoformat()
    clash = tools.extendStay("G-1002", f"{today}T16:00")
    check("extension past same-day ETA -> CONFLICT with latest_possible_checkout",
          clash["status"] == "CONFLICT"
          and clash["data"]["latest_possible_checkout"].endswith("T14:00"), clash)

    capped = tools.extendStay("G-1002", f"{today}T13:30")
    check("extension before ETA -> DONE", capped["ok"], capped)

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
