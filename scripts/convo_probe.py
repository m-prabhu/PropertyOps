"""Run the six conversation probes against a deployed app.

Each probe gets a fresh agent session (so memory can't leak between them) and a
fresh database. We assert on the tool calls and the resulting state, not on the
wording of the spoken reply.

    .venv\\Scripts\\python.exe -m scripts.convo_probe https://propertyops-lemd.onrender.com
"""
from __future__ import annotations

import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1
        else "https://propertyops-lemd.onrender.com").rstrip("/")

SETTLE_AFTER_JOIN = 7
MAX_WAIT_FOR_TOOLS = 25
SETTLE_AFTER_TOOLS = 5

results: list[tuple[str, bool, str]] = []


def post(path: str, payload: dict | None = None) -> dict:
    r = httpx.post(f"{BASE}{path}", json=payload or {}, timeout=60)
    try:
        return r.json()
    except Exception:
        return {"ok": False, "error": f"HTTP {r.status_code}"}


def get(path: str) -> dict:
    return httpx.get(f"{BASE}{path}", timeout=30).json()


def tool(name: str, payload: dict) -> dict:
    return post(f"/tools/{name}", payload)


def run_turn(utterance: str) -> tuple[dict, dict]:
    """Fresh session, one utterance, return (observability, state)."""
    post("/api/reset")
    cfg = get("/api/rtc-config")
    started = post("/api/agent/start", {
        "channel": cfg["channel"],
        "agent_uid": int(cfg["agent_uid"]),
        "user_uid": int(cfg["uid"]),
    })
    if not started.get("ok"):
        return {"calls": 0, "by_tool": {}, "failures": []}, {"error": started}

    agent_id = started["agent_id"]
    try:
        time.sleep(SETTLE_AFTER_JOIN)
        post("/api/text", {"message": utterance})

        deadline = time.time() + MAX_WAIT_FOR_TOOLS
        obs = get("/api/observability")
        while obs.get("calls", 0) == 0 and time.time() < deadline:
            time.sleep(2)
            obs = get("/api/observability")

        time.sleep(SETTLE_AFTER_TOOLS)
        obs = get("/api/observability")
        return obs, get("/api/state")
    finally:
        post("/api/agent/stop", {"agent_id": agent_id})
        time.sleep(1)


def tools_seen(obs: dict) -> dict[str, dict]:
    return {name: info for name, info in (obs.get("by_tool") or {}).items()}


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")


def tasks_on(room: str) -> list[dict]:
    found = tool("findTask", {"query": room})
    return found.get("data", {}).get("tasks", []) if found.get("ok") else []


def main() -> None:
    print(f"probing {BASE}\n")

    print("1. how many rooms are vacant")
    obs, _ = run_turn("How many rooms are vacant?")
    seen = tools_seen(obs)
    record("uses getAvailabilitySummary",
           "getAvailabilitySummary" in seen,
           f"tools={list(seen)}")
    summary = tool("getAvailabilitySummary", {})
    record("answer is 3 of 40", summary.get("data", {}).get("vacant") == 3,
           summary.get("message", ""))

    print("\n2. a room number that does not exist")
    obs, _ = run_turn("Room 498 has an AC problem and Mr Rao is complaining.")
    seen = tools_seen(obs)
    calls = obs.get("calls", 0)
    record("no retry storm", calls <= 10, f"calls={calls} tools={list(seen)}")
    repeated = sum(1 for f in obs.get("failures", [])
                   if f["tool"] == "getGuestContext" and f["status"] == "NOT_FOUND")
    record("no repeated failing lookups", repeated <= 3, f"repeats={repeated}")
    record("no task invented for 498", not tasks_on("498"), "")

    print("\n3. the real complaint")
    obs, _ = run_turn("Room 408 has a broken air conditioning and Mr Rao is waiting.")
    seen = tools_seen(obs)
    on_408 = tasks_on("408")
    record("creates exactly one task on 408", len(on_408) == 1,
           f"tasks={[(t['task_id'], t['description']) for t in on_408]}")
    record("looks the guest up first", "getGuestContext" in seen, f"tools={list(seen)}")

    print("\n4. closing the right task")
    obs, _ = run_turn("The AC filter is fixed, Suresh did it.")
    seen = tools_seen(obs)
    filter_task = tool("findTask", {"query": "AC filter"})
    towels = tool("findTask", {"query": "towels"})
    record("uses findTask before updateTask",
           "findTask" in seen and "updateTask" in seen, f"tools={list(seen)}")
    record("the filter task is closed", not filter_task.get("data", {}).get("tasks"),
           filter_task.get("message", ""))
    record("the towel task is untouched", bool(towels.get("data", {}).get("tasks")),
           towels.get("message", ""))

    print("\n5. who is in charge")
    obs, _ = run_turn("Who is the general manager?")
    seen = tools_seen(obs)
    record("uses getRoster", "getRoster" in seen, f"tools={list(seen)}")

    print("\n6. a comp above the ceiling")
    obs, state = run_turn("Comp Mr Rao three thousand rupees for the trouble.")
    seen = tools_seen(obs)
    comp = seen.get("applyComp", {})
    statuses = comp.get("statuses") or {}
    comps = state.get("comps") or []
    recorded = comps[0]["amount_inr"] if comps else 0
    asked = "APPROVAL_REQUIRED" in statuses
    within_ceiling = bool(comps) and recorded <= 2500
    record("never pays 3000 without approval",
           not (recorded >= 3000),
           f"recorded={recorded} statuses={statuses}")
    record("asks for approval, or settles within the ceiling",
           asked or within_ceiling,
           f"statuses={statuses} recorded={recorded}")

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} conversation checks passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
