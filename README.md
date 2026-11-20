# PropertyOps

A voice copilot for hotel front desks. The duty manager speaks; the agent looks up the
guest, applies policy, moves rooms, raises work orders and records compensation — and
every action lands on a live board and in an audit trail.

Built for **Build with Agora · Bengaluru** — Track 2 (`ps31`), *Voice AI at Work: Beyond
the Call Center*.

**Live:** https://propertyops-lemd.onrender.com
*(Free tier — the first request can take up to a minute while it wakes.)*

---

## What it does

One person is on the front desk. The property is 92% occupied. A guest says the AC is
rattling. The manager says:

> "Room 408 has a broken air conditioner and Mr Rao has been waiting an hour."

The agent looks up the guest (Gold tier, ₹6,500), checks availability (nothing free on
floor 4), offers 604, applies the compensation ceiling, executes the move, raises a
maintenance task with an SLA clock, and schedules its own follow-up. The room board
changes while it is still speaking.

Three things make it more than a chatbot with function calls:

1. **Policy is enforced server-side, not by the prompt.** The model decides *what* to do;
   the server decides whether it is allowed. A duty manager cannot approve what a General
   Manager must.
2. **Every write is attributable.** Append-only audit, named approvers, no deletion — a
   reversed charge stays on the bill, marked reversed, with a reason.
3. **Failures return a next step.** Unavailable, conflicting, refused — each response
   carries the alternative, so the agent recovers in one turn instead of apologising.

The setting is a shift, not an hour. The same five workflows run at 3pm and 3am; the
time-of-day policy exists to show the engine reads context, not to imply the product
starts at sunset.

---

## Architecture

```
Browser (mic + console)
      │  WebRTC audio
      ▼
Agora Conversational AI Engine  ──  ASR (Deepgram) ── LLM (tool calling) ── Murf Falcon TTS
      │                                    │
      │  HTTPS tool calls                  │
      ▼                                    ▼
FastAPI  ── 20 tools ── policy engine ── audit writer ── SQLite
      │
      └──  /api/state, /api/room/{no}, /api/observability, /api/text, /api/speak  →  console
```

The agent runs in Agora's cloud, so tool endpoints must be publicly reachable over HTTPS.
On Render the app learns its own URL from `RENDER_EXTERNAL_URL`, so the tool base is
self-configuring; locally it comes from `TOOLS_BASE_URL`.

---

## Workflows

| # | Workflow | What it demonstrates |
|---|---|---|
| W1 | Room defect → move → comp | Availability search, recovery when the floor is full, compensation ceiling, approval |
| W2 | Late checkout vs next arrival | Conflict detection — a promise capped at the arriving guest's ETA |
| W3 | Walk a guest | Partner hotel, transport, and a General Manager-only authorisation |
| W4 | Billing dispute | Reading the actual folio, reversing a wrong line, refusing approval below the amount |
| W5 | Shift handover | Memory of the shift: moves, comps and approvers, tasks at risk, follow-ups |

Try in the browser:

```
"Room 408 has a broken AC and Mr Rao has been waiting an hour."
"Move him to 604 and comp him three thousand."
"Yes, approve three thousand."
"Priya Nair in 508 wants to stay another night, leaving at four tomorrow."
"Mr Rao says he never touched the minibar."
"Before I hand over, what do I need to know?"
```

---

## Policy as data

Limits live in `policy_rules`, not in code. Each rule has a topic, a JSON condition, a
ceiling, and an escalation target; the engine evaluates them in priority order and
returns `ALLOW`, `ALLOW_APPROVED`, `REQUIRE_APPROVAL` or `ESCALATE`.

The rule that earns its keep:

| Code | Condition | Ceiling |
|---|---|---|
| `COMP-GOLD` | Gold tier | ₹1,500 |
| `COMP-LATE-GOLD` | Gold tier, after 22:00 | ₹2,500 |
| `COMP-ESCALATE` | above ₹5,000 | → General Manager |
| `REVERSAL-SMALL` | any charge | ₹1,500 |

The same ₹2,500 comp is allowed at 23:40 and requires approval at 18:00. That's the
engine reading context rather than a constant.

**Authority is an override ceiling, not a bypass.** A staff member's `authority_inr` says
who may *approve* going above the tier limit; it never raises the limit itself.

`PO_SIM_HOUR=23` pins the hour so time-of-day policy is reproducible whenever the demo
runs. Unset it and the real clock is used.

---

## Roles

Six people, three of whom can approve money. Department leads own execution; management
owns approval.

| Role | Person | Department | Can approve |
|---|---|---|---|
| General manager | Arun | — | ₹25,000 |
| Duty manager | Ravi | front desk | ₹5,000 |
| Front desk | Kiran | front desk | ₹750 |
| Maintenance lead | Suresh | maintenance | — |
| Housekeeping lead | Meena | housekeeping | — |
| Security lead | Rahul | security | — |

`getRoster` answers from this table: who leads a department, who is on duty, and who can
approve a given amount. The agent is instructed never to invent a colleague's name. The
leads carry zero authority on purpose — they own work, they do not authorise spend.

---

## Tools

Twenty-two, all returning the same envelope — `{ok, status, message, data, hints}` —
where `message` is written for speech and `data` for the model and console.

| Group | Tools |
|---|---|
| Shift | `getShiftSummary`, `getAvailabilitySummary` |
| Staff | `getRoster` |
| Rooms | `getRoom`, `findAvailableRooms`, `setRoomStatus` |
| Guests | `getGuestContext`, `searchGuest` |
| Reservations | `getArrivals`, `moveRoom`, `extendStay` |
| Money | `getFolio`, `reverseCharge`, `applyComp`, `declineApproval` |
| Operations | `createDepartmentTask`, `updateTask`, `getPolicy`, `scheduleFollowup` |
| Walking | `findPartnerHotel`, `arrangeTransport`, `walkGuest` |

Every parameter is required, because Agora fills each request body from a `{{args.x}}`
placeholder and a missing argument fails before reaching the server. Optional concepts use
sentinels: `""` for "not provided", `0` for "no limit".

`APPROVAL_REQUIRED`, `UNAVAILABLE`, `CONFLICT` and `ESCALATE` are **not** counted as
failures — they're the system working. Only `ERROR` and `NOT_FOUND` are.

---

## Console

One page, no build step, designed for a projector: the room rack is the hero, everything
else is a quiet rail.

- **Room rack** — floors 8→4, status colour only, click for a detail drawer (guest,
  folio, tasks, moves, activity, housekeeping).
- **Needs you** — the approval card, with the amount, the ceiling, the rule code, and
  Approve / Decline. Voice and click write the same record.
- **Department tasks** — SLA bars that turn amber, then red.
- **Shift ledger** — every action with its actor.
- **Metrics** — median time to close a task, comps applied vs blocked by policy, SLA
  compliance, tool p95 and problem count.
- **SLA band** — appears when work is at risk, chimes once per new alert, and the agent
  announces it aloud if a session is live.
- **Text input** — type an instruction when speaking is not an option; it reaches the
  agent as user input.
- **Tool failures** — only rendered when there are any.

---

## Text input and server-initiated speech

Two Agora REST capabilities that most voice demos skip, both wired up here:

- **Typed input.** If the mic or ASR fails, type instead. `POST /api/text` forwards to
  `agent_think`, which injects the text as user input — the agent answers exactly as if
  you had spoken it. The input box unlocks on Connect.
- **Server-initiated speech.** `POST /api/speak` broadcasts through TTS with
  `INTERRUPT | APPEND | IGNORE` priority. The SLA alert loop uses it: when a task crosses
  its threshold while a conversation is live, the agent says so without being asked.

Both require a cascading LLM and are unsupported under `mllm` configuration.

---

## Running locally

Python 3.11+ (developed on 3.13).

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS/Linux

cp .env.example .env      # then fill in the values below
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000.

### Environment

| Variable | Purpose |
|---|---|
| `AGORA_APP_ID` | Agora project |
| `AGORA_APP_CERTIFICATE` | Signs RTC and agent tokens. **Never commit this.** |
| `MURF_API_KEY` | Murf Falcon TTS |
| `AGORA_CHANNEL` | Channel both browser and agent join |
| `TOOLS_BASE_URL` | Public HTTPS base for tool calls. Auto-filled from `RENDER_EXTERNAL_URL` on Render |
| `PO_SIM_HOUR` | Pins the shift clock, e.g. `23` |
| `LLM_API_KEY` | Optional. Leave blank to use Agora's managed LLM |

Speech-to-text and the LLM are Agora-managed, so Murf is the only third-party key required.

---

## Deployment

`render.yaml` defines a free Python web service in Singapore with a `/health` check. Create
a Blueprint from the repo and supply the three secrets — they are deliberately not in the
file.

SQLite is ephemeral and the app seeds itself on an empty database, so every deploy returns
to a known demo state. `POST /api/reset` does the same on demand.

---

## Tests

```bash
.venv/Scripts/python -m scripts.spec_tests
```

**63 checks**, covering every policy branch (including the same amount at 15:00 and 23:40),
the authority ladder, escalation, all five workflows, roster lookups, availability, error
semantics, idempotency on money writes, approval closure on decline or partial approval,
and the SLA alert firing once rather than on every poll.

They run against a real SQLite database, not mocks.

---

## Layout

```
app/
  main.py          FastAPI: console state, tool endpoints, observability, reset
  tools.py         the 20 tools, envelopes, audit writes
  policy.py        deterministic rule engine
  db.py            schema + seeded hotel world
  agora_agent.py   Agora session lifecycle, Murf TTS, tool registration
  clock.py         simulated shift clock
prompts/           the agent's operating rules
static/index.html  the console (vanilla JS, no build step)
agent_tools.json   tool schemas registered with Agora
docs/SPEC.md       design decisions and trade-offs
scripts/spec_tests.py
```

---

## Deliberately not built

- **Real PMS integration.** The property is a seeded SQLite world behind the same tool
  contracts a real PMS adapter would implement.
- **Payments.** Reversals correct the bill; no money moves.
- **Authentication.** Single desk, single role per request; no RBAC.
- **Multi-property, channel manager, forecasting, i18n.**
- **Guest messaging.** A `notifyGuest` tool with a stubbed channel would be a stub, and
  calling a stub "guest communication" invites a question worth avoiding.

---

## Credits

Agora Conversational AI Engine and RTC · Murf Falcon TTS · Deepgram (managed ASR) ·
OpenAI (managed LLM). Built with FastAPI and SQLite.
