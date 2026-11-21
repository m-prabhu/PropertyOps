# PropertyOps — Hackathon Submission

## 1. Project name and description

**PropertyOps — Voice AI Ops Copilot for Hotel Front Desks**

PropertyOps is a voice assistant for the hotel front desk. The person on duty just speaks
about a problem — AC not working, guest arguing about the bill, guest arrived but no room.
The assistant does the actual work: finds the guest, checks which rooms are free, applies the
hotel rules, moves the guest, books a cab, creates work orders and records compensation.
Everything shows up on a live board, and every action is written into an audit trail with who
did it and why.

The difference from a normal voice bot is simple. **The rules are checked on our server, not
inside the AI prompt.** The AI decides what to do; the server decides whether it is allowed.
Money above ₹500 cannot be recorded until a person reads a four digit code from the screen.
And walking a guest to another hotel can only be done with the General Manager's approval.

## 2. Selected problem statement

**Track 2 — Voice AI at Work: Beyond the Call Center** (`ps31`)

> *How can real-time voice AI improve workplace productivity by enabling employees to access
> information, resolve issues, automate repetitive tasks, and complete business workflows
> through natural, hands-free conversations?*

We took the simple reading of this: one person at the desk, middle of the shift, hands busy.
That person does not need another chatbot to answer questions about a policy book. They need
the decision taken, the record written, and the follow-up fixed.

## 3. GitHub repository

`https://github.com/m-prabhu/PropertyOps`

Public repository. The full source, the seeded hotel data and both test suites are in there.
`README.md` explains how to run it locally and how the policy engine and tool layer fit
together.

## 4. Working prototype

`https://propertyops-lemd.onrender.com`

It is hosted on Render's free plan, so the first open can take up to one minute while it
wakes up. After that it works normally. If you want the original state back, click **Reset
demo state**.

There is nothing to install or configure. Open the link, press Connect, allow the microphone,
and start talking.

## 5. Agora Conversational AI integration

**How it fits together:** the browser joins the same audio channel as the agent. The agent
runs inside Agora's cloud. Our tools run on our own server.

**On the server we use the `agora-agents` Python SDK (version 2.14.0):**

- `AsyncAgora` keeps the app id and certificate, and uses the certificate to create tokens on
  the server itself.
- The agent is built from three parts: `DeepgramSTT` (with `nova-3`) to understand speech,
  `OpenAI` (with `gpt-4o-mini`) to think, and `MurfTTS` to speak.
- The session is created with `create_async_session`, and we keep the `agent_id` so we can
  stop it later. When the browser disconnects the microphone, the agent is stopped too.
- We tuned the turn detection so it does not cut the person off: `interrupt_duration_ms=160`,
  `prefix_padding_ms=300`, `silence_duration_ms=480`.

**Tools.** We tell the AI about 23 functions. When the AI wants one, Agora calls our HTTPS
endpoint (for example `/tools/moveRoom`) and gives the result back to the AI. Only the AI's
final sentence goes to the voice — not the raw tool output. That is why every tool returns a
`message` written for speaking, and a separate `data` part for the AI and the console.

The tool address comes from `RENDER_EXTERNAL_URL`, so once it is deployed the agent finds our
server by itself. No tunnel, no manual URL.

**On the browser side**, the console joins the channel with the Agora Web SDK using a numeric
UID and publishes the microphone. The token is created with `generate_convo_ai_token` and is
valid for one hour.

**Two more Agora features we use:**

| Feature | What we use it for |
|---|---|
| `agents.speak` | The agent speaks on its own: when a task is about to cross its SLA time, it announces it without being asked |
| `agent_management.agent_think` | Typed input: if the microphone has a problem, we can type the instruction and the agent treats it as if we spoke |

Both of these need a ConvoAI token per call, and our code creates that token using the App
Certificate.

## 6. Murf AI Falcon TTS integration

Murf Falcon is the voice of the agent.

| Setting | Value |
|---|---|
| Vendor | `murf` |
| Model | `FALCON` |
| Voice | `Abhinav` (Indian English, male) |
| Locale | `en-IN` |
| Endpoint | `wss://global.api.murf.ai/v1/speech/stream-input` |
| Sample rate | 24 kHz |
| Key | `MURF_API_KEY` |

**How it works:** the AI writes the sentence, Agora sends it to Murf, Murf sends the audio
back, and the audio plays in the browser. Murf never sees our hotel data, the guest details
or the policy text — only the sentence it has to speak.

The same voice is used for the automatic SLA announcements, so the agent sounds the same
whether it is answering us or speaking on its own. We chose `FALCON` because it starts
speaking quickly and because it pronounces Indian names, room numbers and rupee amounts
properly.

## 7. Agentic workflows and actions performed

There are five workflows. All of them can be done by voice, and all of them are recorded.

| # | Workflow | Tools used, in order | What it shows |
|---|---|---|---|
| W1 | AC complaint → move room → compensation | `getGuestContext` → `findAvailableRooms` → `getPolicy` → `moveRoom` → `applyComp` → `createDepartmentTask` → `scheduleFollowup` | Many steps in one go, with a human approval in the middle |
| W2 | Late checkout vs next arrival | `getGuestContext` → `getArrivals` → `getPolicy` → `extendStay` | It checks the next guest's arrival and refuses to promise beyond that time |
| W3 | Walking a guest | `findAvailableRooms` → `findPartnerHotel` → `arrangeTransport` → `walkGuest` | The last-resort action, and it goes to a higher authority |
| W4 | Bill dispute | `getFolio` → `reverseCharge` | It reads the real bill and corrects a wrong charge |
| W5 | Shift handover | `getShiftSummary` | It remembers its own shift |

**W1 in the demo's own words:** a guest in room 408 has a broken AC and has been waiting for
one hour. The assistant looks him up — Gold tier, ₹6,500 — sees that nothing is free on the
fourth floor, offers room 604, and tells us the ceiling is ₹2,500 after 10pm. It moves him,
so 408 becomes out of service and 604 becomes occupied on the board. Then it raises an
approval card for ₹3,000. Nothing is recorded until we read the four digit code from the
screen. After that it records the compensation, creates a maintenance task with a 30 minute
SLA, and sets its own follow-up.

**What the assistant cannot do, by design:**

- It cannot approve its own money. Above ₹500, a compensation needs the code that is shown
  only on the screen. The AI can type any name; it cannot guess a number it was never told.
- It cannot walk a guest. That needs the General Manager.
- It cannot quietly reduce the amount to avoid approval.
- It cannot delete anything. A corrected charge stays on the bill, marked as reversed, with
  the reason.

**Reliability is also part of the product.** Every failure comes back with a reason and the
next option — room not available, conflict, not found, approval needed. Duplicate work orders
are refused. When someone says a room number that does not exist, the assistant replies with
the correct range and does not retry again and again; earlier this was looping 18 times, and
that fix came from testing.

## 8. Demo video

`PropertyOps-demo.mp4` — around 7 minutes 30 seconds.

Five acts, recorded in one take with live narration: the AC complaint and the approval code,
the late checkout that gets refused, walking a guest, the minibar dispute, and the shift
handover at the end.
