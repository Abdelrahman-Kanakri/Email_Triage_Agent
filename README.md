# Email Triage Agent

A LangGraph agent that reads an inbox, screens every email for prompt injection, drafts replies to the clean ones, and sends nothing without a human's approval.

```text
START ─► unauthenticated ─► auth_tools ─► reading ─► inbox_tools ─► guard ─┬─► drafting ─► awaiting_approval ─► sending ─┐
              ▲  (interrupt: grant access?)                          ▲      │                 │ edit ↺   reject ─► drafting │
              └──── denied: model explains, END                      └──────┴── flagged / next email ◄────────────────────┘
```

The full edge map, with what each router reads, is in [`app/graph/build.py`](app/graph/build.py) and in the "Phase 2- Step 5" section of [`email-triage-state-machine.excalidraw`](email-triage-state-machine.excalidraw).

## How it works

| Stage | Node | What it does | LLM? |
|---|---|---|---|
| Access | `unauthenticated` → `auth_tools` | The model requests `auth_tool_access`. The tool pauses with `interrupt()` until a human grants or denies access. | Decides only |
| Fetch | `reading` → `inbox_tools` | The model requests `fetch_inbox`. The tool loads `*.md` emails, skipping any already handled on this thread. | Decides only |
| Screen | `guard` | Two tiers. A regex match on known injection patterns is trusted immediately. If the regex finds nothing, an LLM classifier labels the email `clean` / `injection` / `ambiguous`. Flagged and empty emails are recorded and skipped. | Classifier + reason |
| Draft | `drafting` | Writes the subject and body only. The recipient is always the original sender and is never taken from model output. | Yes |
| Approve | `awaiting_approval` | `interrupt()` with three choices: **approve** → send; **edit** → the human's text, then ask again; **reject** + reason → redraft. | No |
| Send | `sending` | Writes the reply to `OUTBOX_PATH` (a stand-in for a real mail provider). No model can ever call this function. | No |

### Safety properties
- **Least privilege.** Each FSM state binds only its own tool ([`registry.py`](app/tools/registry.py)). `send_email` is not a tool at all.
- **Untrusted content stays data.** Email bodies are wrapped in `<email_content>` tags in every prompt. Addresses used for sending come from parsed headers, never from model output.
- **Fail closed.** Any resume value other than an explicit `approve` counts as a denial.
- **PII never logged in plaintext.** A structlog processor replaces every email address with its HMAC-SHA256 pseudonym ([`pii.py`](app/guardrails/pii.py)).

## Run it

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # then fill in the keys
```

| Surface | Command | Notes |
|---|---|---|
| Terminal | `uv run python -m app.main` | Interactive. In-memory state, so one process is one session. |
| Web UI + API | `uv run uvicorn app.api.server:app --port 8000` → http://localhost:8000 | Threads persist in SQLite (`CHECKPOINT_DB`). |
| Docker | `docker compose up --build` | Inbox mounted read-only; outbox and checkpoints on named volumes. |

### API
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/health` | – | `{"status":"ok"}` (no auth) |
| POST | `/api/threads` | – | `{"thread_id"}` |
| GET | `/api/threads/{id}` | – | Snapshot: pending interrupt, flagged emails, processed ids, conversation |
| POST | `/api/threads/{id}/messages` | `{"text"}` | SSE stream |
| POST | `/api/threads/{id}/resume` | `{"resume": {...}}` | SSE stream |

- **SSE events:** `node`, `message`, `flagged`, `sent`, `interrupt` (payload has a `kind`), `done`, `error`.
- **Resume payloads by `kind`:**

| `kind` | Resume payload |
|---|---|
| `auth` | `{"decision": "approve"\|"deny"}` |
| `approval` | `{"decision": "approve"\|"edit"\|"reject"}` |
| `edit` | `{"subject", "body"}` |
| `reject_reason` | `{"reason"}` |

- **Errors:**
  - `409`: a run is already in progress on that thread, or the call doesn't match the thread's paused/not-paused state.
  - `422`: malformed body.
  - `401`: `APP_API_KEY` is set and the `X-API-Key` header is missing or wrong.

## Tests

```bash
uv run pytest            # 93 tests, no network: every LLM is a scripted fake
uv run ruff check app tests
```

| File | Covers |
|---|---|
| `test_routing.py` | Every router, including stale `current_email`, empty inbox, and "never route back to `reading`" |
| `test_hitl_gate.py` | The auth interrupt through a real `ToolNode`; fail-closed resume values |
| `test_injection.py` | Regex tier vs LLM tier; the old `;`/`--` false positives |
| `test_guard.py` | Clean, flagged and empty-body branches |
| `test_nodes_draft_approve_send.py` | Drafting, the approval `Command` routing, multi-interrupt resume order, send pinning |
| `test_fetch_inbox.py` | Parsing, sort order, dedup of processed emails |
| `test_graph_e2e.py` | Whole-graph trajectories: reject → redraft → edit → approve, denial, empty or all-flagged inbox, re-triage on the same thread |
| `test_api.py` | SSE over HTTP, 409/422/401, persistence across an app restart |
| `test_pii.py` | Pseudonymization in kwargs and free text |

## Observability
- **LangSmith tracing** is on when `LANGSMITH_TRACING=true`. Every run is named `email-triage` and tagged with its surface (`cli`/`api`). `thread_id` is in the metadata, so you can filter traces per conversation.
- **Structured JSON logs:** one file per module under `logs/`, plus stdout when `LOG_TO_STDOUT=true` (the default in Docker). The API binds a `request_id` to every log line written while serving a request, and echoes it in the `X-Request-ID` response header. Each request also gets one access-log line with its status and duration.
- **Health:** `GET /health`, wired into the Docker `HEALTHCHECK`.

## Known limits
- **Spam and phishing are not flagged.** The guard detects attacks on the *AI*, not junk mail aimed at a human, so emails 04 and 05 get polite replies. A separate spam classifier would be the next step.
- **Single worker by design.** Per-thread run locks live in process memory, and SQLite has a single writer. To scale out, move to the Postgres checkpointer plus a shared lock.
- **Sending is simulated:** replies are written to `data/outbox/`. A real provider (Gmail/SMTP) would replace `send_email`'s file write.
- **Unused dependencies make the image larger.** `presidio-*` and `guardrails-ai` are in `pyproject.toml` but unused (PII redaction is plain regex). Run `uv remove presidio-analyzer presidio-anonymizer guardrails-ai` to drop spaCy and shrink the image a lot.
