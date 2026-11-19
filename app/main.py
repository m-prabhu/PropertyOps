"""FastAPI host: console state, tool endpoints for the Agora agent, reset."""
from __future__ import annotations

import json
import os
import random
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db as dbmod
from . import agora_agent as agora
from .clock import now as clock_now
from .tools import REGISTRY

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

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
    """Render health check. Kept cheap: no DB, no Agora calls."""
    return {"ok": True, "service": "propertyops"}


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


@app.post("/api/agent/stop")
async def stop_agent(req: AgentStop) -> dict:
    try:
        await agora.service.stop(req.agent_id)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.get("/api/state")
def state() -> dict:
    conn = dbmod.connect()
    try:
        rooms = [dict(r) for r in conn.execute(
            "SELECT room_no, floor, category, status, housekeeping, rate_inr FROM rooms "
            "ORDER BY floor DESC, room_no")]
        tasks = [dict(r) for r in conn.execute(
            "SELECT task_id, department, location, priority, description, status, sla_due "
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
    finally:
        conn.close()

    now = datetime.now()
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
        "stats": {
            "occupancy_pct": round(occupied / total * 100, 1) if total else 0,
            "out_of_service": oos,
            "open_tasks": sum(1 for t in tasks if t["status"] in ("open", "in_progress")),
            "comp_total_inr": comp_total,
        },
    }


class ToolCall(BaseModel):
    class Config:
        extra = "allow"


@app.post("/tools/{name}")
def call_tool(name: str, payload: ToolCall) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        raise HTTPException(404, f"Unknown tool '{name}'")
    try:
        return fn(**payload.model_dump())
    except TypeError as exc:
        return {"ok": False, "status": "ERROR",
                "message": f"Bad arguments for {name}: {exc}", "data": {}, "hints": {}}
    except Exception as exc:  # the agent should hear the failure, not a stack trace
        return {"ok": False, "status": "ERROR", "message": str(exc), "data": {}, "hints": {}}


@app.post("/api/reset")
def reset() -> dict:
    dbmod.init(reset=True)
    return {"status": "reset", "rooms": 40}


class Decision(BaseModel):
    by: str = "R. Menon"


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
