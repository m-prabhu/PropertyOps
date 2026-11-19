"""Deterministic policy engine.

The LLM proposes; this module decides. Rules come from the `policy_rules` table so
limits are data, not code. No network, no randomness: same input always gives the
same decision, which is what makes the approval gate credible on stage.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .clock import now as clock_now
from .db import connect

# Approval authority ladder. A role can only override up to its ceiling.
ROLE_AUTHORITY = {"front_desk": 750, "duty_manager": 5000, "gm": 25000}


def _ctx_amount(ctx: dict) -> int:
    try:
        return int(ctx.get("amount_inr") or 0)
    except (TypeError, ValueError):
        return 0


def _matches(cond: dict, ctx: dict) -> bool:
    for key, expected in cond.items():
        if key == "amount_gt":
            if not _ctx_amount(ctx) > expected:
                return False
        elif key == "amount_lte":
            if not _ctx_amount(ctx) <= expected:
                return False
        elif key == "wait_gt":
            if not int(ctx.get("wait_minutes") or 0) > expected:
                return False
        elif key == "after_hour":
            local = ctx.get("local_time") or clock_now()
            if local.hour < expected:
                return False
        else:
            if ctx.get(key) != expected:
                return False
    return True


def _required_role(amount: int) -> str | None:
    if amount <= ROLE_AUTHORITY["front_desk"]:
        return "front_desk"
    if amount <= ROLE_AUTHORITY["duty_manager"]:
        return "duty_manager"
    return "gm"


def evaluate(topic: str, ctx: dict) -> dict[str, Any]:
    """Return a Decision for `topic` given the caller's context."""
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM policy_rules WHERE topic = ? AND active = 1 ORDER BY priority DESC, id ASC",
            (topic,),
        )]
    finally:
        conn.close()

    matched = []
    for row in rows:
        cond = json.loads(row["condition_json"] or "{}")
        if _matches(cond, ctx):
            matched.append((row, len(cond)))

    if not matched:
        return {
            "decision": "ALLOW", "rule_code": None, "limit_inr": None,
            "required_role": None, "escalate_to": None,
            "message": "No policy rule covers this. Proceed only with manager approval.",
        }

    # priority already ordered by SQL; specificity breaks ties within a priority band
    best, _ = max(matched, key=lambda m: (m[0]["priority"], m[1]))
    amount = _ctx_amount(ctx)
    base = {
        "rule_code": best["code"],
        "limit_inr": best["limit_inr"],
        "required_role": best["requires_role"],
        "escalate_to": best["escalate_to"],
        "message": best["message"],
    }

    # Escalation rules have no ceiling; matching them means "this goes upstairs".
    if best["limit_inr"] is None and best["escalate_to"]:
        return {"decision": "ESCALATE", **base,
                "message": f"{amount} INR: {best['message']}"}

    limit = int(best["limit_inr"] or 0)
    if amount <= limit:
        return {"decision": "ALLOW", **base}

    # Above the ceiling: a named human may override, up to their own authority.
    approved_by = ctx.get("approved_by")
    if approved_by:
        conn = connect()
        try:
            staff = conn.execute(
                "SELECT name, role, authority_inr FROM staff "
                "WHERE lower(name) = lower(?) AND active = 1", (approved_by,)
            ).fetchone()
        finally:
            conn.close()
        if staff and amount <= staff["authority_inr"]:
            return {"decision": "ALLOW_APPROVED", **base,
                    "approved_by": staff["name"], "approved_role": staff["role"],
                    "message": f"Approved by {staff['name']} ({staff['role']})."}
        who = staff["name"] if staff else approved_by
        ceiling = staff["authority_inr"] if staff else 0
        return {"decision": "REQUIRE_APPROVAL", **base,
                "required_role": _required_role(amount),
                "message": (
                    f"{who} can approve up to {ceiling} INR, which does not cover {amount} INR. "
                    f"{amount} INR needs a {_required_role(amount)}."
                )}

    return {"decision": "REQUIRE_APPROVAL", **base,
            "required_role": _required_role(amount),
            "message": (
                f"{amount} INR is above the {limit} INR ceiling ({best['code']}). "
                f"{best['message']}"
            )}


def tier_ceiling(tier: str, local_time: datetime | None = None) -> dict:
    """The comp ceiling that currently applies to a tier, including time-of-day rules."""
    d = evaluate("comp", {"tier": tier, "amount_inr": 0, "local_time": local_time or clock_now()})
    return {"limit_inr": d.get("limit_inr"), "rule_code": d.get("rule_code")}
