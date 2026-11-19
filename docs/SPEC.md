# PropertyOps â€” Technical Spec (v1, P0)

**Product:** PropertyOps â€” Voice AI Ops Copilot for Hotel Front Desks
**Track:** Build with Agora BLR Â· **Track 2 / `ps31`** â€” Voice AI at Work: Beyond the Call Center
**Status:** Draft for approval. No implementation starts until this is signed off.
**Date:** 2026-11-19

---

## 0. Scope decision

One property, one role (duty manager), voice-first, mock PMS. "Complete" means
*bounded and internally consistent*, not feature-comparable with Opera or Mews.

| Tier | Contents | Ship by |
|---|---|---|
| **P0** | 14 tools Â· W1, W2, W5 Â· policy engine Â· console Â· audit Â· reset | 21 Nov, 12:45 IST |
| **P1** | W3 (walk), W4 (billing dispute), guest notify, text fallback, proactive SLA alert, MCP wrapper | only if on schedule |
| **P2** | Real PMS integration, payments, multi-property, RBAC, housekeeping app, forecasting, full i18n | roadmap slide only |

**Non-goals, stated explicitly:** no real PMS, no payment gateway, no auth, no
multi-tenant, no IoT. These are named on the roadmap slide, not faked in the build.

---

## 1. Architecture

```
 Browser (mic + console)
        â”‚  WebRTC audio
        â–¼
 Agora Conversational AI Engine â”€â”€ ASR â”€â”€ LLM (tool calling) â”€â”€ Murf Falcon TTS
        â”‚                              â”‚
        â”‚  HTTPS tool calls            â”‚ transcript events
        â–¼                              â–¼
 FastAPI  â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”¬â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€  Console API (/api/state)
   tools/               â”‚
   policy engine        â”‚
   audit writer         â”‚
        â–¼               â–¼
               SQLite (single file)
```

**Hard rule:** the LLM decides *what* to do; the server decides *whether it is allowed*.
No policy limit, price, or approval is ever enforced by the prompt.

Sanity checks:
- Tool p95 latency target: **< 300 ms**.
- Tool response payload cap: **1 KB**, max 8 list items.
- Agent time-to-first-audio target: **< 900 ms**.
- Console state poll: 1 s (SSE is P1).

---

## 2. Data model (target schema)

We control the seed and have no production data, so this replaces the current
schema outright. No migrations. `POST /api/reset` re-seeds.

### Core

```sql
property(id, name, timezone, currency, room_count)

rooms(
  room_no PK, floor, category,            -- deluxe | premier | suite
  status CHECK IN ('vacant','occupied','blocked','out_of_service'),
  housekeeping CHECK IN ('clean','dirty','inspected'),
  rate_inr, features, note
)

guests(
  guest_id PK, name, tier CHECK IN ('silver','gold','platinum'),
  phone, email, preferences, language DEFAULT 'en-IN', notes
)

reservations(
  res_id PK, guest_id FK, room_no FK NULL,   -- NULL until assigned
  arrival_date, departure_date, arrival_eta, -- ETA drives W2 conflict math
  booked_category, rate_plan, rate_inr,
  status CHECK IN ('expected','in_house','checked_out','no_show','walked')
)

folio_charges(
  charge_id PK, res_id FK, category, description,
  amount_inr, posted_at,
  status CHECK IN ('posted','reversed'), reversed_reason
)
```

### Operations

```sql
tasks(
  task_id PK, created_at, department, location, priority,
  description, status CHECK IN ('open','in_progress','done','cancelled'),
  assignee, sla_due, closed_at, note
)

comps(
  comp_id PK, created_at, guest_id, amount_inr, reason,
  rule_code, approved_by, request_id UNIQUE NULL   -- idempotency
)

moves(id PK, created_at, guest_id, from_room, to_room, reason, request_id)

followups(id PK, created_at, room_no, due_at, note,
          status CHECK IN ('pending','done','missed'))

policy_rules(
  id PK, code UNIQUE, topic,
  condition_json,            -- {"tier":"gold"} | {"amount_gt":5000}
  limit_inr, requires_role, escalate_to, message,
  priority,                  -- explicit ordering; escalation rules sit at 100
  active
)

staff(staff_id PK, name, role, authority_inr, active)   -- two roles + front desk

audit(
  id PK, created_at, actor,        -- 'agent' | 'staff:<name>' | 'system'
  action, entity, entity_id, detail_json
)
```

`policy_chunks` stays for prose retrieval (`getPolicy`); it feeds the *spoken*
explanation, never the enforcement.

_Indexes:_ `reservations(room_no, status)`, `tasks(status, sla_due)`,
`audit(created_at)`, `policy_rules(topic, active)`.

### Seed invariants (the demo world)

| Invariant | Value | Why |
|---|---|---|
| Occupancy | 92.5% (37/40) | Scarcity forces real trade-offs |
| Vacant | 604, 702, 803 | 604 = same-category fallback; 702/803 = upgrade pressure |
| Floor 4 | full except the incident room | Makes "same floor" impossible â†’ recovery |
| Incident room | 408, Mr. Arjun Rao, Gold, â‚¹6,500 | Tier drives the comp ceiling |
| Next arrival on 508 | today, ETA 14:00, deluxe | Makes W2 a genuine conflict |
| Dormant accounts | 1 platinum, 2 silver | Lets tier behaviour be demonstrated |
| Staff | 1 duty manager (â‚¹5,000), 1 GM (â‚¹25,000), 1 front desk (â‚¹750) | Escalation is demonstrable |

---

## 3. Policy engine

Rules are **data**. The LLM may propose; the engine decides.

### Evaluation

```
evaluate(topic, ctx) -> Decision
  ctx = { tier, amount_inr, wait_minutes, local_time, from_category, to_category, role }
```

1. Select `active` rules where `topic` matches.
2. Keep rules whose `condition_json` **all** match `ctx` (exact match for strings,
   `amount_gt` / `amount_lte` / `wait_gt` / `after_hour` for numerics).
3. Order by `priority DESC`, then specificity (most conditions matched), then `id`.
4. Outcome:
   - rule has no ceiling and names an `escalate_to` â†’ **ESCALATE**
   - `amount_inr <= limit_inr` â†’ **ALLOW**
   - above the ceiling, `approved_by` names a staff member whose
     `authority_inr >= amount_inr` â†’ **ALLOW_APPROVED**
   - above the ceiling, no valid approver â†’ **REQUIRE_APPROVAL**

**Authority is an override ceiling, not a bypass.** `authority_inr` never raises the
tier limit by itself; it only decides who may override it. This is why a front desk
agent cannot approve a Gold comp that the duty manager could.

| Role | Authority |
|---|---|
| `front_desk` | â‚¹750 |
| `duty_manager` | â‚¹5,000 |
| `gm` | â‚¹25,000 |

```python
Decision = {
  "decision": "ALLOW | ALLOW_APPROVED | REQUIRE_APPROVAL | ESCALATE",
  "rule_code": "COMP-GOLD",
  "limit_inr": 1500,
  "required_role": "duty_manager",
  "escalate_to": None,
  "message": "Gold guests are covered to 1500 INR without further approval."
}
```

### Seed rules

| code | topic | condition | limit | escalate |
|---|---|---|---|---|
| `COMP-SILVER` | comp | tier=silver | 750 | â€” |
| `COMP-GOLD` | comp | tier=gold | 1500 | â€” |
| `COMP-PLAT` | comp | tier=platinum | 3000 | â€” |
| `COMP-LATE-GOLD` | comp | tier=gold, after_hour=22 | 2500 | â€” |
| `COMP-ESCALATE` | comp | amount_gt=5000 | â€” | GM |
| `LC-WAIVE` | late_checkout | â€” | 0 | â€” |
| `RATE-CHANGE` | rate_change | â€” | 0 | â€” |
| `ESCALATE-PLAT` | escalation | tier=platinum | â€” | GM |

`COMP-LATE-GOLD` is the interesting one: the same â‚¹2,500 comp is allowed at 23:40
and requires approval at 18:00. It proves the engine reads context, not a constant.

**Determinism requirement:** `evaluate()` is pure Python. No LLM call, no randomness,
same input â†’ same output. It is unit-testable and it is tested.

---

## 4. Tool contract

### Envelope (every tool, success or failure)

```json
{
  "ok": true,
  "status": "DONE",
  "message": "Moved Mr Rao to 604. 408 is out of service.",
  "data": { },
  "hints": { "next": ["createDepartmentTask"] }
}
```

`status` âˆˆ `DONE Â· APPROVAL_REQUIRED Â· ESCALATE Â· UNAVAILABLE Â· CONFLICT Â· NOT_FOUND Â· ERROR`

Rules:
- `message` is written for **speech**, one or two sentences, no field names, no JSON.
- `data` is for the console and the agent's reasoning â€” never read aloud verbatim.
- Every failure must include a next step in `hints.next` or `data.available_now`.
- Writes accept optional `request_id`; a repeat with the same id returns the
  original result instead of double-charging.

### P0 tools (14)

| # | Tool | Arguments | Returns |
|---|---|---|---|
| 1 | `getShiftSummary` | `scope?` | moves, comps, open tasks, SLA-at-risk, followups due, escalations |
| 2 | `getRoom` | `room_no` | room status, housekeeping, rate, features, current guest id |
| 3 | `findAvailableRooms` | `category, floor?, features?` | `matches_on_floor`, `elsewhere`, `upgrades`, `occupancy_pct` |
| 4 | `setRoomStatus` | `room_no, status, reason` | new status; refuses `vacant` while a defect task is open |
| 5 | `getGuestContext` | `room_no` | guest, tier, rate, comps-to-date, authority, open tasks |
| 6 | `searchGuest` | `query` | â‰¤5 matches by name/phone |
| 7 | `getArrivals` | `room_no?, date` | expected arrivals with ETA and booked category |
| 8 | `moveRoom` | `guest_id, from_room, to_room, reason, request_id?` | new room; old room â†’ `out_of_service` |
| 9 | `extendStay` | `guest_id, new_departure, rate_inr?, request_id?` | updated departure; flags next-arrival conflict |
| 10 | `applyComp` | `guest_id, amount_inr, reason, approved_by?, request_id?` | `APPROVAL_REQUIRED` or `DONE` + rule code |
| 11 | `createDepartmentTask` | `department, location, priority, description` | task id + SLA due |
| 12 | `updateTask` | `task_id, status, note?` | updated task |
| 13 | `getPolicy` | `topic` | â‰¤2 prose sections (for quoting, not enforcing) |
| 14 | `scheduleFollowup` | `room_no, minutes, note` | due_at |

**P1 tools (deferred):** `get_folio`, `reverse_charge`, `notify_guest`,
`find_partner_hotel`, `arrange_transport`, `check_in`, `check_out`.

### Error semantics that the agent depends on

| Case | Status | Must include |
|---|---|---|
| Target room not vacant | `UNAVAILABLE` | `data.available_now` |
| Comp above ceiling | `APPROVAL_REQUIRED` | `limit_inr`, `requested_inr`, `required_role` |
| Extension collides with arrival | `CONFLICT` | arrival ETA, `latest_possible_checkout` |
| Room with open defect â†’ vacant | `CONFLICT` | `open_task_ids` |
| Unknown room/guest | `NOT_FOUND` | closest matches |

This table is a feature. It is what makes the agent recover instead of apologise.

---

## 5. Workflows

### W1 â€” Room defect â†’ move â†’ comp  *(P0)*

```
trigger  "408's AC is rattling, guest waited an hour"
  â”œâ”€ getGuestContext(408)            â†’ Rao, gold, â‚¹6,500, ceiling â‚¹1,500
  â”œâ”€ findAvailableRooms(deluxe, 4)   â†’ 0 on floor 4 â†’ 604 (f6), upgrades 702/803
  â”œâ”€ getPolicy("comp limit gold")     â†’ quotes â‚¹1,500
  â”œâ”€ moveRoom(...â†’604)                â†’ 408 out_of_service, 604 occupied
  â”œâ”€ applyComp(2500)                  â†’ APPROVAL_REQUIRED
  â”‚    â””â”€ [voice] manager approves     â†’ applyComp(..., approved_by="R. Menon")
  â”œâ”€ createDepartmentTask(maintenance, 408, high, â€¦)
  â””â”€ scheduleFollowup(408, 30, "confirm AC repaired")
```
**Branch:** comp â‰¤ ceiling â†’ no approval, straight through. **Branch:** amount > â‚¹5,000
â†’ `ESCALATE` to GM, no comp recorded. **Branch:** room re-sold while talking â†’ `UNAVAILABLE`,
agent offers the next option without restarting the flow.

**Written by:** agent. **Approved by:** staff. **Audited:** both, separately.

### W2 â€” Late checkout vs next arrival  *(P0)*

```
trigger  "508 wants to keep the room till 4pm"
  â”œâ”€ getGuestContext(508)
  â”œâ”€ getArrivals(room_no=508, date=today)
  â”‚    â”œâ”€ no arrival that day â†’ extendStay() â†’ confirm
  â”‚    â””â”€ arrival ETA 14:00 â†’ latest_possible_checkout = 14:00
  â”œâ”€ getPolicy("late checkout")
  â””â”€ branches
       a) within cap                  â†’ extendStay(new_departure â‰¤ 14:00)
       b) guest wants 16:00           â†’ CONFLICT; offer (i) hold only to 14:00,
                                        (ii) move to a vacant room for the afternoon,
                                        (iii) decline + goodwill
       c) waive the fee               â†’ applyComp(amount = LC fee, approver required)
```

**Signed-off rule: the cap is the arrival ETA itself.** No turnover buffer is modelled.
`latest_possible_checkout` is returned in the `CONFLICT` payload so the agent can offer
the exact alternative rather than a vague no. If the property later needs a cleaning
buffer, it becomes a policy rule â€” not a constant in code.

### W5 â€” Shift handover  *(P0)*

```
trigger  "what do I need to know?"
  â””â”€ getShiftSummary()
       â†’ moves made, comps + approver, open tasks with minutes-to-SLA,
         followups due, escalations pending, out-of-service rooms
     read back in â‰¤ 20 seconds, ordered by urgency, nothing invented
```
Read-only, no writes. This is the workflow that makes the product feel like a
product: it has memory of its own shift.

### W3 / W4  *(P1, build only if ahead)*

- **W3 walk a guest:** partner hotel + transport + comp + GM escalation.
- **W4 billing dispute:** folio line items + reversal + goodwill.

---

## 6. Agent design

**Prompt structure** (four blocks, in this order):
1. Identity & audience â€” duty manager, not guest; voice, not chat.
2. Hard operating rules â€” look before acting, never invent policy, never claim
   availability you didn't fetch, never self-approve.
3. Tool discipline â€” one lookup before any write; confirm before multi-step execution.
4. Voice style â€” short sentences, no field names, no "I have updated the database".

**Confirmation policy**

| Action | Readback required | Explicit "yes" required |
|---|---|---|
| Read-only lookup | no | no |
| Room move | yes (room + guest) | no |
| Task creation | no | no |
| Comp â‰¤ ceiling | yes (amount + guest) | no |
| Comp > ceiling | yes (amount + ceiling + reason) | **yes** |
| Escalation to GM | yes | **yes** |
| Status â†’ out_of_service | yes | no |

**Latency tactics**
- Tool payloads capped at 1 KB; never return the room list when two rooms answer the question.
- Speak the plan while the next tool runs ("checking the fourth floorâ€¦").
- Never chain more than 3 tools without a spoken progress sentence.
- Pre-warm the agent connection before the demo; keep the tunnel alive.

**Spoken vs visual division**
Voice carries decisions and confirmations. The console carries numbers, history and
status. Don't read the room board aloud; don't hide a â‚¹2,500 comp in a sentence.

---

## 7. Console (information architecture)

Single page, no build step, 1440Ã—900 target, projector-legible.

| Region | Contents | Update |
|---|---|---|
| Header | property, shift window, occupancy, OOS, open tasks, comps total, mic status | 1 s |
| Room board | floors 8â†’4, 8 rooms/row, status colour + housekeeping mark, click â†’ drawer | 1 s |
| Task queue | department, room, priority, **SLA countdown**, owner | 1 s |
| Activity timeline | every tool call: time, actor, action, outcome. Policy blocks in red | 1 s |
| Approval panel | appears on `APPROVAL_REQUIRED`: amount, ceiling, rule code, guest. **Clickable and speakable** â€” the manager can say "yes, approve" or press Approve; both paths make the same server call with `approved_by` | event |
| Metrics strip | median resolve time, comps applied vs blocked, SLA compliance % | 5 s |
| Transport bar | mic connect, **text input fallback**, reset demo | â€” |

**Design constraints** (fed to `frontend-design`):
- Density over decoration: the room board must fit without scrolling.
- Colour is semantic only â€” occupied / vacant / out-of-service / blocked. No gradients.
- Numerals must be readable at 3 m: â‰¥ 24 px for room numbers and amounts.
- Motion only to signal state change (a room flipping status), never on load.
- Empty and error states are written as direction, not mood.

**Explicitly rejected:** dark background + single acid-green accent (the current
build), card-everything SaaS kit, decorative gradients.

---

## 8. Non-functional

| Concern | Decision |
|---|---|
| **Audit** | Append-only. Every write tool emits one row with `actor`. Comp rows carry `approved_by`. Immutable â€” no update/delete path. |
| **Idempotency** | `request_id` on all money/move writes; duplicate returns original result. |
| **Determinism** | Policy engine is pure; demo resets to identical state via `/api/reset`. |
| **Recoverability** | Every failure returns a next step. No dead-end tool responses. |
| **Fallback** | Text input channel if mic/ASR fails; pre-recorded demo video as last resort. |
| **Observability** | Tool latency, status distribution, and failed calls logged to console. |
| **Data safety** | SQLite file; nothing leaves the machine except audio to Agora and text to the LLM. |
| **Shift clock** | `PO_SIM_HOUR` (0â€“23) pins the hour so time-of-day policy is reproducible regardless of when the demo runs. Unset = real clock. |

---

## 9. Build plan

| Phase | Work | Acceptance |
|---|---|---|
| **P1** (5h) | Schema rewrite + seed + policy engine + 14 tool stubs returning correct envelopes | `scripts/spec_tests.py` passes: rule evaluation, error semantics, idempotency |
| **P2** (5h) | W1 + W2 fully wired; console room board + task queue + audit | Both workflows complete via HTTP, state visibly changes |
| **P3** (4h) | Console redesign (design pass â†’ build â†’ guidelines review) + approval panel + metrics | Console passes accessibility review; board fits 1440Ã—900 |
| **P4** (4h) | Voice loop end to end; latency tuning; W5 handover | Full three-workflow conversation with no manual intervention |
| **P5** (5h) | Rehearsal Ã—10, fallback recording, README, roadmap slide | Submitted by 12:45 IST |

**Cut order if behind:** text fallback â†’ proactive SLA alert â†’ W4 â†’ W3 â†’ MCP wrapper â†’
metrics strip. Policy engine and audit are never cut.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Murf not available as an Agora TTS vendor | **Blocks the voice layer** | Verify first; fallback TTS + note in README |
| Agora cloud can't reach `localhost` | Tools never fire | Tunnel (`cloudflared`) from hour one, not hour ten |
| LLM tool-call reliability | Broken demo | Short prompts, â‰¤14 tools, explicit enums, deterministic error envelopes |
| Hall audio / ASR noise | Demo failure | Headset mic, short turns, text fallback, recorded video |
| Scope creep from "complete product" | Nothing finishes | P0 list is frozen at sign-off; P1 only after P5 starts early |

---

## 11. Decisions (signed off 2026-11-19)

| # | Decision | Ruling |
|---|---|---|
| 1 | W2 conflict policy | **Cap the extension at the arrival ETA.** No turnover buffer. `CONFLICT` returns `latest_possible_checkout`. |
| 2 | Approval parity | **Clickable and speakable.** Either path writes the same `approved_by` record. |
| 3 | Shift identity | **Two-role staff table** (duty manager â‚¹5,000 / GM â‚¹25,000) plus a front desk at â‚¹750, so escalation and refusal are both demonstrable. |
| 4 | Tool count | **14 tools for P0, confirmed.** P1 tools stay out until P0 is green. |

## 12. Verification

`scripts/spec_tests.py` is the acceptance gate for Phase P1 and covers:

- every policy branch, including the same amount at 15:00 vs 23:40
- authority override vs refusal (duty manager vs front desk)
- escalation above â‚¹5,000
- `UNAVAILABLE`, `CONFLICT`, `NOT_FOUND` payload contracts
- idempotency of `moveRoom` and `applyComp` on `request_id`
- room cannot return to `vacant` with an open defect task
- W2 cap behaviour on both sides of the ETA
- W5 handover message contains no JSON and no field names
- simulated shift clock raises the Gold ceiling at 23:00

Run: `.venv\Scripts\python.exe -m scripts.spec_tests`
