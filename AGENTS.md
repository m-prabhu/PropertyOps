# PropertyOps — project notes

Voice ops copilot for hotel front desks. FastAPI + SQLite + static console, with an
Agora Conversational AI agent (Murf Falcon TTS).

## Commands

```powershell
.\.venv\Scripts\python.exe -m scripts.spec_tests              # 64 checks, must pass
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Deployed on Render (Singapore, free tier). `POST /api/reset` restores the seeded hotel.

## Text encoding — read before bulk edits

PowerShell 5.1 destroys non-ASCII in this repo. Do **not** use
`Get-Content`/`Set-Content`/`Out-File` on any file containing `₹`, em dashes, or arrows;
it double-encodes them (`₹` → `â‚¹`) and `-Encoding utf8` adds a BOM that breaks
`agent_tools.json`.

Use Python with explicit `encoding="utf-8"`, or the edit/write tools. If mojibake
appears, revert with `git checkout -- <file>` and redo it properly. The full rule lives
in the global `~/.config/opencode/AGENTS.md`.

## Conventions

- Tools return one envelope: `{ok, status, message, data, hints}`. `message` is written
  for speech; `data` is for the model and the console.
- Every tool parameter is required, because Agora fills request bodies from
  `{{args.x}}` placeholders. Optional concepts use sentinels: `""` and `0`.
- Policy limits live in the `policy_rules` table, never in code or the prompt, and the
  approval ladder recognises only `front_desk`, `duty_manager`, `gm`.
- `schema_version` in the `meta` table gates the schema. Bump it when the schema
  changes; `init()` drops and reseeds rather than migrating.
- The room drawer and the tool surface must expose the same facts. When the drawer
  shows something new, add it to a tool in the same change.
- Keep the demo reproducible: names are first-name only, and all seeded data is
  deterministic across resets.
