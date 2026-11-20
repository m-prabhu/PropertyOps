"""FastAPI host: console state, tool endpoints for the Agora agent, reset."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db as dbmod
from . import agora_agent as agora
from .clock import now as clock_now
from .tools import REGISTRY, at_risk_tasks

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

logger = logging.getLogger("uvicorn.error")

# Every tool call observed this process, newest last. Bounded so a long-running
# demo can't grow it without limit.
TOOL_LOG: deque = deque(maxlen=500)


def _record_tool(name: str, result: dict, duration_ms: int, args: dict) -> None:
    entry = {
        "ts": clock_now().strftime("%H:%M:%S"),
        "tool": name,
        "status": result.get("status", "ERROR"),
        "ok": bool(result.get("ok")),
        "ms": duration_ms,
        "args": {k: str(v)[:60] for k, v in args.items()},
        "message": str(result.get("message", ""))[:200],
    }
    TOOL_LOG.append(entry)
    # The literal "logged to console" half of the requirement.
    logger.info("tool=%s status=%s ms=%d ok=%s", name, entry["status"], duration_ms, entry["ok"])


def _tool_health() -> dict:
    rows = list(TOOL_LOG)
    per_tool: dict[str, dict] = {}
    for r in rows:
        b = per_tool.setdefault(r["tool"], {"count": 0, "ms": [], "statuses": Counter()})
        b["count"] += 1
        b["ms"].append(r["ms"])
        b["statuses"][r["status"]] += 1

    for b in per_tool.values():
        values = sorted(b["ms"])
        n = len(values)
        b["p50_ms"] = values[n // 2]
        b["p95_ms"] = values[min(n - 1, int(round(0.95 * (n - 1))))]
        b["statuses"] = dict(b["statuses"])
        del b["ms"]

    durations = sorted(r["ms"] for r in rows)
    # Approvals, conflicts and unavailability are expected business outcomes, not
    # failures. Only a broken call or a lookup that found nothing counts.
    problem_statuses = {"ERROR", "NOT_FOUND"}
    problems = [r for r in rows if r["status"] in problem_statuses]
    overall_p95 = (durations[min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))]
                   if durations else None)

    return {
        "calls": len(rows),
        "p95_ms": overall_p95,
        "errors": sum(1 for r in rows if r["status"] == "ERROR"),
        "problems": len(problems),
        "non_done": sum(1 for r in rows if not r["ok"]),
        "by_tool": per_tool,
        "failures": problems[-8:],
    }


app = FastAPI(title="PropertyOps")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.on_event("startup")
def _startup() -> None:
    dbmod.init()


def _readable(detail_json: str) -> str:
    try:
        d = json.loads(detail_json)
    except Exception:
        return detail_json or ""
    if isinstance(d, dict):
        if set(d) == {"detail"}:
            return str(d["detail"])
        return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in d.items() if v is not None)
    return str(d)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
def config() -> dict:
    return {
        "appId": os.getenv("AGORA_APP_ID", ""),
        "channel": os.getenv("AGORA_CHANNEL", "propertyops-demo"),
        "token": os.getenv("AGORA_TOKEN") or None,
    }


@app.get("/health")
def health() -> dict:
    """Render health check. Cheap: no DB, no Agora calls.

    Includes the deployed commit so a push can be confirmed end to end.
    """
    commit = (os.getenv("RENDER_GIT_COMMIT") or "local")[:7]
    return {"ok": True, "service": "PropertyOps", "commit": commit}


@app.get("/api/voice-status")
def voice_status() -> dict:
    """What still blocks a working voice loop, if anything."""
    return {"ready": agora.service.ready(), "missing": agora.service.missing()}


@app.get("/api/rtc-config")
def rtc_config(channel: str | None = None, uid: int | None = None) -> dict:
    ch = channel or os.getenv("AGORA_CHANNEL", "propertyops-demo")
    user_uid = uid if uid and uid > 0 else random.randint(1000, 9999999)
    try:
        return {"ok": True, **agora.service.rtc_config(ch, user_uid)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class AgentStart(BaseModel):
    channel: str
    agent_uid: int
    user_uid: int


@app.post("/api/agent/start")
async def start_agent(req: AgentStart) -> dict:
    try:
        return {"ok": True, **await agora.service.start(
            channel=req.channel, agent_uid=req.agent_uid, user_uid=req.user_uid)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "missing": agora.service.missing()}


class AgentStop(BaseModel):
    agent_id: str


class TextMessage(BaseModel):
    message: str


@app.post("/api/text")
async def text_input(body: TextMessage) -> dict:
    """Typed input, injected as if the manager had said it."""
    text = (body.message or "").strip()
    if not text:
        return {"ok": False, "error": "Type something first."}
    try:
        return {"ok": True, **await agora.service.think(text)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/speak")
async def speak(body: TextMessage) -> dict:
    """Make the agent say something unprompted. Used for SLA alerts and for testing."""
    text = (body.message or "").strip()
    if not text:
        return {"ok": False, "error": "Nothing to say."}
    try:
        return {"ok": True, **await agora.service.speak(text, priority="INTERRUPT")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/agent/stop")
async def stop_agent(req: AgentStop) -> dict:
    try:
        await agora.service.stop(req.agent_id)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _metrics(conn) -> dict:
    """The three numbers the spec asks for: resolve time, policy blocks, SLA."""
    rows = [dict(r) for r in conn.execute(
        "SELECT created_at, closed_at, sla_due FROM tasks")]
    closed = [r for r in rows if r.get("closed_at")]
    durations = sorted(
        (datetime.fromisoformat(r["closed_at"]) - datetime.fromisoformat(r["created_at"]))
        .total_seconds() / 60 for r in closed)
    median = None
    if durations:
        mid = len(durations) // 2
        median = (durations[mid] if len(durations) % 2
                  else (durations[mid - 1] + durations[mid]) / 2)

    now = clock_now()
    breached = 0
    for r in rows:
        end = datetime.fromisoformat(r["closed_at"]) if r.get("closed_at") else now
        if end > datetime.fromisoformat(r["sla_due"]):
            breached += 1

    total = len(rows)
    return {
        "median_close_min": round(median) if median is not None else None,
        "tasks_closed": len(closed),
        "comps_applied": conn.execute("SELECT COUNT(*) c FROM comps").fetchone()["c"],
        "comps_blocked": conn.execute(
            "SELECT COUNT(*) c FROM audit WHERE action = 'comp_blocked'").fetchone()["c"],
        "tasks_total": total,
        "tasks_breached": breached,
        "sla_compliance_pct": round((total - breached) / total * 100) if total else 100,
    }


def _raise_alerts(threshold: int = 15) -> tuple[list[dict], list[dict]]:
    """Flag tasks at risk of breaching SLA, once each, and write them to the ledger.

    Returns (all_alerting_tasks, newly_alerting_tasks).
    """
    items = at_risk_tasks(threshold)
    if not items:
        return [], []
    fresh: list[dict] = []
    conn = dbmod.connect()
    try:
        for a in items:
            seen = conn.execute("SELECT 1 FROM sla_alerts WHERE task_id = ?",
                                (a["task_id"],)).fetchone()
            if seen:
                continue
            conn.execute(
                "INSERT INTO sla_alerts (task_id, created_at, minutes_left) VALUES (?,?,?)",
                (a["task_id"], dbmod.now_iso(), a["minutes_left"]))
            conn.execute(
                "INSERT INTO audit (created_at, actor, action, entity, entity_id, detail_json) "
                "VALUES (?,?,?,?,?,?)",
                (dbmod.now_iso(), "system", "sla_alert", "task", str(a["task_id"]),
                 json.dumps({"department": a["department"], "location": a["location"],
                             "minutes_left": a["minutes_left"]})))
            fresh.append(a)
        conn.commit()
    finally:
        conn.close()
    return items, fresh


def _alert_line(task: dict) -> str:
    minutes = task["minutes_left"]
    when = f"{minutes} minutes left" if minutes >= 0 else f"{abs(minutes)} minutes over"
    return (f"Heads up: {task['department']} on {task['location']} has {when} on its SLA.")


def _broadcast_alert(fresh: list[dict]) -> None:
    """Make the running agent say it, without blocking the state request."""
    if not fresh or not agora.service.current_agent_id:
        return
    try:
        asyncio.create_task(agora.service.speak(_alert_line(fresh[0]), priority="APPEND"))
    except RuntimeError:  # no running loop; the console still shows the alert
        pass


@app.get("/api/alerts")
def alerts() -> dict:
    items, _ = _raise_alerts()
    return {"alerts": items}


@app.get("/api/state")
async def state() -> dict:
    # Raise alerts before reading the audit log, so the banner and its ledger
    # entry appear in the same response.
    alerts, fresh = _raise_alerts()
    _broadcast_alert(fresh)
    conn = dbmod.connect()
    try:
        rooms = [dict(r) for r in conn.execute(
            "SELECT room_no, floor, category, status, housekeeping, rate_inr FROM rooms "
            "ORDER BY floor DESC, room_no")]
        tasks = [dict(r) for r in conn.execute(
            "SELECT task_id, department, location, priority, description, status, sla_due, assignee "
            "FROM tasks ORDER BY status, sla_due ASC LIMIT 12")]
        comps = [dict(r) for r in conn.execute(
            "SELECT c.comp_id, c.amount_inr, c.approved_by, c.rule_code, c.created_at, g.name "
            "FROM comps c JOIN guests g ON g.guest_id = c.guest_id ORDER BY c.comp_id DESC LIMIT 8")]
        audit = [dict(r) for r in conn.execute(
            "SELECT created_at, actor, action, entity, detail_json FROM audit "
            "ORDER BY id DESC LIMIT 25")]
        followups = [dict(r) for r in conn.execute(
            "SELECT room_no, due_at, note, status FROM followups ORDER BY id DESC LIMIT 8")]
        staff = [dict(r) for r in conn.execute(
            "SELECT name, role, authority_inr FROM staff WHERE active = 1 ORDER BY authority_inr")]
        approvals = [dict(r) for r in conn.execute(
            "SELECT a.id, a.created_at, a.guest_id, a.amount_inr, a.reason, a.rule_code, "
            "g.name, r.room_no FROM approvals a "
            "JOIN guests g ON g.guest_id = a.guest_id "
            "LEFT JOIN reservations r ON r.guest_id = a.guest_id AND r.status = 'in_house' "
            "WHERE a.status = 'pending' ORDER BY a.id")]
        total = len(rooms)
        occupied = sum(1 for r in rooms if r["status"] == "occupied")
        oos = sum(1 for r in rooms if r["status"] == "out_of_service")
        comp_total = conn.execute(
            "SELECT COALESCE(SUM(amount_inr),0) s FROM comps").fetchone()["s"]
        metrics = _metrics(conn)
    finally:
        conn.close()

    now = clock_now()
    for t in tasks:
        try:
            t["minutes_to_sla"] = int(
                (datetime.fromisoformat(t["sla_due"]) - now).total_seconds() // 60)
        except (TypeError, ValueError):
            t["minutes_to_sla"] = 0

    for a in audit:
        a["ts"] = a.pop("created_at")
        a["detail"] = _readable(a.pop("detail_json"))

    return {
        "now": clock_now().strftime("%H:%M"),
        "rooms": rooms,
        "tasks": tasks,
        "comps": comps,
        "audit": audit,
        "followups": followups,
        "staff": staff,
        "approvals": approvals,
        "metrics": metrics,
        "tools": _tool_health(),
        "alerts": alerts,
        "stats": {
            "occupancy_pct": round(occupied / total * 100, 1) if total else 0,
            "out_of_service": oos,
            "open_tasks": sum(1 for t in tasks if t["status"] in ("open", "in_progress")),
            "comp_total_inr": comp_total,
        },
    }


@app.get("/api/room/{room_no}")
def room_detail(room_no: str) -> dict:
    """Everything the drawer shows for one room."""
    conn = dbmod.connect()
    try:
        room = conn.execute("SELECT * FROM rooms WHERE room_no = ?", (room_no,)).fetchone()
        if room is None:
            raise HTTPException(404, f"Unknown room {room_no}")
        res = conn.execute(
            """SELECT r.res_id, r.arrival_date, r.departure_date, r.rate_inr, r.status,
                      g.name, g.tier, g.phone
               FROM reservations r JOIN guests g ON g.guest_id = r.guest_id
               WHERE r.room_no = ? AND r.status = 'in_house'""", (room_no,)).fetchone()
        tasks = [dict(t) for t in conn.execute(
            "SELECT task_id, department, priority, description, status, sla_due, assignee "
            "FROM tasks WHERE location = ? ORDER BY status, sla_due", (room_no,))]
        moves = [dict(m) for m in conn.execute(
            "SELECT created_at, from_room, to_room, reason FROM moves "
            "WHERE from_room = ? OR to_room = ? ORDER BY id DESC LIMIT 5", (room_no, room_no))]
        charges = []
        if res:
            charges = [dict(c) for c in conn.execute(
                "SELECT description, amount_inr, status FROM folio_charges "
                "WHERE res_id = ? ORDER BY charge_id DESC LIMIT 5", (res["res_id"],))]
        events = [dict(e) for e in conn.execute(
            "SELECT created_at, actor, action, detail_json FROM audit "
            "WHERE entity_id = ? ORDER BY id DESC LIMIT 6", (room_no,))]
    finally:
        conn.close()

    return {
        "room": dict(room),
        "guest": dict(res) if res else None,
        "tasks": tasks,
        "moves": moves,
        "charges": charges,
        "events": [{**e, "detail": _readable(e["detail_json"])} for e in events],
    }


class ToolCall(BaseModel):
    class Config:
        extra = "allow"


@app.post("/tools/{name}")
def call_tool(name: str, payload: ToolCall) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        _record_tool(name, {"ok": False, "status": "NOT_FOUND",
                            "message": f"Unknown tool '{name}'"}, 0, payload.model_dump())
        raise HTTPException(404, f"Unknown tool '{name}'")

    args = payload.model_dump()
    started = time.perf_counter()
    try:
        result = fn(**args)
    except TypeError as exc:
        result = {"ok": False, "status": "ERROR",
                  "message": f"Bad arguments for {name}: {exc}", "data": {}, "hints": {}}
    except Exception as exc:  # the agent should hear the failure, not a stack trace
        result = {"ok": False, "status": "ERROR", "message": str(exc),
                  "data": {}, "hints": {}}

    _record_tool(name, result, round((time.perf_counter() - started) * 1000), args)
    return result


@app.get("/api/observability")
def observability() -> dict:
    return _tool_health()


@app.post("/api/reset")
def reset() -> dict:
    dbmod.init(reset=True)
    TOOL_LOG.clear()
    return {"status": "reset", "rooms": 40}


class Decision(BaseModel):
    by: str = "Ravi"


@app.post("/api/approvals/{approval_id}/approve")
def approve(approval_id: int, body: Decision) -> dict:
    """The clickable half of approval parity: same server call the voice path makes."""
    from . import tools as toolsmod

    conn = dbmod.connect()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE id = ? AND status = 'pending'",
                           (approval_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "No pending approval with that id")

    result = toolsmod.applyComp(row["guest_id"], row["amount_inr"], row["reason"],
                                 approved_by=body.by)
    if result["ok"]:
        conn = dbmod.connect()
        try:
            conn.execute(
                "UPDATE approvals SET status = 'approved', decided_by = ?, decided_at = ? "
                "WHERE id = ?", (body.by, dbmod.now_iso(), approval_id))
            conn.commit()
        finally:
            conn.close()
    return result


@app.post("/api/approvals/{approval_id}/decline")
def decline(approval_id: int, body: Decision) -> dict:
    conn = dbmod.connect()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE id = ? AND status = 'pending'",
                           (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No pending approval with that id")
        conn.execute(
            "UPDATE approvals SET status = 'declined', decided_by = ?, decided_at = ? WHERE id = ?",
            (body.by, dbmod.now_iso(), approval_id))
        conn.execute(
            "INSERT INTO audit (created_at, actor, action, entity, entity_id, detail_json) "
            "VALUES (?,?,?,?,?,?)",
            (dbmod.now_iso(), f"staff:{body.by}", "comp_declined", "guest", row["guest_id"],
             json.dumps({"amount_inr": row["amount_inr"], "reason": row["reason"]})))
        conn.commit()
    finally:
        conn.close()
    return {"status": "DECLINED", "message": "Declined. Nothing was recorded against the guest."}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
