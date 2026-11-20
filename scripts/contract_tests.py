"""HTTP contract tests for the tool endpoints.

No LLM, no audio, no Agora session — every call goes over HTTP the way the
agent's tool executor would, and we assert the shapes and error semantics.

    .venv\\Scripts\\python.exe -m scripts.contract_tests
    .venv\\Scripts\\python.exe -m scripts.contract_tests https://your-app.onrender.com
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1
        else os.getenv("CONTRACT_BASE", "http://127.0.0.1:8000")).rstrip("/")

ENVELOPE_KEYS = {"ok", "status", "message", "data", "hints"}
BUSINESS_OK = {"DONE", "APPROVAL_REQUIRED", "UNAVAILABLE", "ESCALATE", "CONFLICT", "NOT_FOUND", "ERROR"}

fails: list[str] = []
statuses: list[int] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  -> {detail}")
        fails.append(name)


def call(tool: str, payload: dict) -> tuple[int, dict | None]:
    response = httpx.post(f"{BASE}/tools/{tool}", json=payload, timeout=25)
    statuses.append(response.status_code)
    try:
        return response.status_code, response.json()
    except Exception:
        return response.status_code, None


def envelope_ok(body: dict | None) -> bool:
    return (
        isinstance(body, dict)
        and ENVELOPE_KEYS <= set(body)
        and isinstance(body.get("message"), str)
        and body["message"].strip() != ""
        and isinstance(body.get("status"), str)
        and body["status"] in BUSINESS_OK
        and isinstance(body.get("data"), dict)
        and isinstance(body.get("hints"), dict)
    )


def speakable(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    message = body.get("message", "")
    return len(message) <= 400 and "{" not in message and "_inr" not in message


def main() -> None:
    try:
        httpx.get(f"{BASE}/health", timeout=10)
    except Exception as exc:
        print(f"cannot reach {BASE}: {exc}")
        sys.exit(2)

    httpx.post(f"{BASE}/api/reset", timeout=20)

    # --- 1. every tool returns a well-formed envelope -------------------
    print("\n== envelope contract")
    simple = {
        "getShiftSummary": {},
        "getAvailabilitySummary": {},
        "getRoster": {"query": "", "min_authority_inr": 0},
        "getRoom": {"room_no": "408"},
        "findAvailableRooms": {"category": "deluxe", "floor": 0, "features": ""},
        "getGuestContext": {"room_no": "408"},
        "searchGuest": {"query": "Rao"},
        "getArrivals": {"room_no": "", "date": ""},
        "getFolio": {"guest_id": "G-1001"},
        "getPolicy": {"topic": "comp"},
        "findTask": {"query": ""},
        "findPartnerHotel": {"category": "deluxe", "max_distance_km": 0},
        "scheduleFollowup": {"room_no": "408", "minutes": 10, "note": "contract"},
    }
    for tool, payload in simple.items():
        code, body = call(tool, payload)
        check(f"{tool}: 200 + envelope", code == 200 and envelope_ok(body), (code, body))
        check(f"{tool}: message is speakable", speakable(body), body)

    # writes with side effects, in dependency order
    code, body = call("createDepartmentTask",
                      {"department": "maintenance", "location": "Contract",
                       "priority": "low", "description": "contract check"})
    check("createDepartmentTask: 200 + envelope", code == 200 and envelope_ok(body), (code, body))
    task_id = (body or {}).get("data", {}).get("task_id")

    code, body = call("updateTask", {"task_id": task_id, "status": "done", "note": ""})
    check("updateTask: 200 + envelope", code == 200 and envelope_ok(body), (code, body))
    check("updateTask: confirmation names the task",
          "contract check" in (body or {}).get("message", "").lower(), body)

    code, body = call("moveRoom", {"guest_id": "G-1001", "from_room": "408",
                                   "to_room": "604", "reason": "contract"})
    check("moveRoom: 200 + envelope", code == 200 and envelope_ok(body), (code, body))

    code, body = call("extendStay", {"guest_id": "G-1002", "new_departure": "2026-12-30"})
    check("extendStay: 200 + envelope", code == 200 and envelope_ok(body), (code, body))

    code, body = call("applyComp", {"guest_id": "G-1001", "amount_inr": 100,
                                    "reason": "contract", "approved_by": ""})
    check("applyComp: 200 + envelope", code == 200 and envelope_ok(body), (code, body))

    code, body = call("declineApproval", {"guest_id": "G-1001", "reason": "contract",
                                          "declined_by": ""})
    check("declineApproval: 200 + envelope", code == 200 and envelope_ok(body), (code, body))

    code, body = call("arrangeTransport", {"guest_id": "G-2003", "partner_id": "P-01",
                                           "minutes": 10})
    transport_id = (body or {}).get("data", {}).get("transport_id")
    code, body = call("walkGuest", {"guest_id": "G-2003", "partner_id": "P-01",
                                    "transport_id": transport_id, "reason": "contract",
                                    "approved_by": ""})
    check("walkGuest: 200 + envelope", code == 200 and envelope_ok(body), (code, body))

    httpx.post(f"{BASE}/api/reset", timeout=20)
    code, body = call("getFolio", {"guest_id": "G-1001"})
    charges = (body or {}).get("data", {}).get("charges", [])
    if charges:
        code, body = call("reverseCharge", {"charge_id": charges[-1]["charge_id"],
                                            "reason": "contract", "approved_by": ""})
        check("reverseCharge: 200 + envelope", code == 200 and envelope_ok(body), (code, body))

    # --- 2. hostile input ------------------------------------------------
    print("\n== hostile input")
    hostile = [
        ("unknown room", "getGuestContext", {"room_no": "9999"}, "NOT_FOUND"),
        ("empty room number", "getGuestContext", {"room_no": ""}, "NOT_FOUND"),
        ("unknown task id", "updateTask", {"task_id": 999999, "status": "done", "note": ""}, "NOT_FOUND"),
        ("bogus task status", "updateTask", {"task_id": 1, "status": "exploded", "note": ""}, "ERROR"),
        ("unknown charge", "reverseCharge", {"charge_id": 999999, "reason": "x", "approved_by": ""}, "NOT_FOUND"),
        ("bogus charge id", "reverseCharge", {"charge_id": "abc", "reason": "x", "approved_by": ""}, "ERROR"),
        ("bogus room status", "setRoomStatus", {"room_no": "408", "status": "on fire", "reason": "x"}, "ERROR"),
        ("bad date", "extendStay", {"guest_id": "G-1001", "new_departure": "next tuesday"}, "ERROR"),
        ("zero comp", "applyComp", {"guest_id": "G-1001", "amount_inr": 0, "reason": "x", "approved_by": ""}, "ERROR"),
        ("negative comp", "applyComp", {"guest_id": "G-1001", "amount_inr": -50, "reason": "x", "approved_by": ""}, "ERROR"),
        ("unknown guest", "applyComp", {"guest_id": "G-9999", "amount_inr": 100, "reason": "x", "approved_by": ""}, "NOT_FOUND"),
        ("unknown partner", "findPartnerHotel", {"category": "penthouse", "max_distance_km": 0}, "ERROR"),
        ("no matching task", "findTask", {"query": "zzzzzz"}, "NOT_FOUND"),
        ("walk without transport", "walkGuest", {"guest_id": "G-2003", "partner_id": "P-01",
                                                 "transport_id": 999999, "reason": "x", "approved_by": ""}, "NOT_FOUND"),
    ]
    for name, tool, payload, expected in hostile:
        code, body = call(tool, payload)
        check(f"{name} -> {expected}",
              code == 200 and envelope_ok(body) and body["status"] == expected, (code, body))

    # --- 3. no server errors ---------------------------------------------
    print("\n== transport")
    missing = httpx.post(f"{BASE}/tools/notATool", json={}, timeout=15)
    statuses.append(missing.status_code)
    check("unknown tool -> 404", missing.status_code == 404, missing.status_code)

    incomplete = httpx.post(f"{BASE}/tools/getRoom", json={}, timeout=15)
    statuses.append(incomplete.status_code)
    check("missing required argument is rejected, not crashed",
          incomplete.status_code in (200, 422), incomplete.status_code)

    check("no request ever returned 5xx", all(s < 500 for s in statuses),
          sorted({s for s in statuses if s >= 500}))

    print("\n" + ("ALL CONTRACT CHECKS PASSED" if not fails else f"{len(fails)} FAILED: {fails}"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
