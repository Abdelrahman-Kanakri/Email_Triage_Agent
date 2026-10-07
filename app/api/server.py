"""FastAPI server: run the triage graph per thread and stream it over SSE.

    uv run uvicorn app.api.server:app --port 8000

Endpoints (all under /api need `X-API-Key` when APP_API_KEY is set):

    GET  /health                       liveness probe (no auth)
    POST /api/threads                  -> {"thread_id"}
    GET  /api/threads/{id}             snapshot: pending interrupt, flagged, sent, ...
    POST /api/threads/{id}/messages    {"text"}   -> SSE stream of the run
    POST /api/threads/{id}/resume      {"resume"} -> SSE stream (answers an interrupt)
    GET  /                             the web UI (app/ui/index.html)

SSE event types: `node` (a node finished), `message` (agent text),
`flagged` (an email was flagged), `sent` (a reply was written),
`interrupt` (run paused, needs a human), `done` (run reached END),
`error`.

Why POST + SSE instead of WebSockets: each request is one run segment
that ends at END or at an interrupt, so a one-way stream per request is
enough, and plain HTTP is easier to proxy and test.
"""

import asyncio
import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from app.core import get_logger, settings
from app.graph.build import build_graph, initial_state, run_config, sqlite_checkpointer

logger = get_logger(__name__)
UI_FILE = Path(__file__).resolve().parent.parent / "ui" / "index.html"


# ── Schemas ─────────────────────────────────────────────────────────────
class MessageIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class ResumeIn(BaseModel):
    """Exactly what the paused node expects, e.g. {"decision": "approve"},
    {"subject": ..., "body": ...} or {"reason": ...}."""

    # min_length=1: LangGraph treats an empty resume as "no resume", which
    # would silently leave the run paused.
    resume: dict[str, str] = Field(..., min_length=1)


class ThreadOut(BaseModel):
    thread_id: str


# ── App wiring ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the persistent checkpointer once; build the graph on top of it."""
    async with sqlite_checkpointer(settings.CHECKPOINT_DB) as saver:
        app.state.graph = build_graph(checkpointer=saver)
        # One lock per thread: two concurrent runs on the same checkpoint
        # would interleave writes. A second request gets 409 instead.
        app.state.locks = {}
        logger.info("api started", checkpoint_db=settings.CHECKPOINT_DB)
        yield
    logger.info("api stopped")


app = FastAPI(title="Email Triage Agent", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Bind a request id into structlog's contextvars so every log line
    emitted while serving this request carries it; log one access line."""
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path != "/health":
        logger.info(
            "http request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
    return response


def require_api_key(request: Request) -> None:
    """No-op unless APP_API_KEY is configured. Constant-time compare."""
    expected = settings.APP_API_KEY
    if expected is None:
        return
    given = request.headers.get("X-API-Key", "")
    if not secrets.compare_digest(given, expected.get_secret_value()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def get_graph(request: Request) -> CompiledStateGraph:
    return request.app.state.graph


GraphDep = Annotated[CompiledStateGraph, Depends(get_graph)]


def _lock_for(request: Request, thread_id: str) -> asyncio.Lock:
    return request.app.state.locks.setdefault(thread_id, asyncio.Lock())


# ── Serialization helpers ───────────────────────────────────────────────
def _sse(event: str, data: Any) -> dict:
    return {"event": event, "data": json.dumps(data, default=str)}


def _snapshot(values: dict, interrupts: tuple) -> dict:
    """The JSON view of a thread the UI renders."""
    draft = values.get("draft")
    current = values.get("current_email")
    return {
        "authenticated": bool(values.get("authenticated")),
        "pending_interrupt": interrupts[0].value if interrupts else None,
        "current_email": current.model_dump() if current else None,
        "draft": draft.model_dump() if draft else None,
        "inbox_remaining": len(values.get("inbox") or []),
        "processed_ids": values.get("processed_ids") or [],
        "flagged": [
            {
                "message_id": f.email.message_id,
                "subject": f.email.subject,
                "sender": f.email.sender,
                "category": f.category,
                "reason": f.reason,
            }
            for f in values.get("flagged_emails") or []
        ],
        "conversation": [
            {
                "role": "user" if isinstance(m, HumanMessage) else "agent",
                "content": m.content,
            }
            for m in values.get("messages") or []
            if isinstance(m, HumanMessage | AIMessage) and m.content
        ],
    }


async def _stream_run(
    graph: CompiledStateGraph, payload: Any, thread_id: str, lock: asyncio.Lock
) -> AsyncIterator[dict]:
    """Run one segment of the graph and translate updates into SSE events."""
    config = run_config(thread_id, surface="api")
    async with lock:
        try:
            async for chunk in graph.astream(payload, config, stream_mode="updates"):
                for node, update in chunk.items():
                    if node == "__interrupt__":
                        continue
                    yield _sse("node", {"node": node})
                    if not isinstance(update, dict):
                        continue
                    for msg in update.get("messages", []):
                        if isinstance(msg, AIMessage) and msg.content:
                            yield _sse("message", {"content": msg.content})
                    for f in update.get("flagged_emails", []):
                        yield _sse(
                            "flagged",
                            {
                                "subject": f.email.subject,
                                "category": f.category,
                                "reason": f.reason,
                            },
                        )
                    if node == "sending":
                        yield _sse("sent", {"message_id": update["processed_ids"][0]})

            snap = await graph.aget_state(config)
            if snap.interrupts:
                yield _sse("interrupt", snap.interrupts[0].value)
            else:
                yield _sse("done", _snapshot(snap.values, snap.interrupts))
        except Exception as exc:
            logger.exception("run failed", thread_id=thread_id)
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"})


# ── Routes ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def ui() -> FileResponse:
    return FileResponse(UI_FILE)


@app.post("/api/threads", response_model=ThreadOut, dependencies=[Depends(require_api_key)])
async def create_thread() -> ThreadOut:
    return ThreadOut(thread_id=str(uuid.uuid4()))


@app.get("/api/threads/{thread_id}", dependencies=[Depends(require_api_key)])
async def get_thread(thread_id: str, graph: GraphDep) -> dict:
    snap = await graph.aget_state(run_config(thread_id, surface="api"))
    return _snapshot(snap.values or {}, snap.interrupts)


@app.post("/api/threads/{thread_id}/messages", dependencies=[Depends(require_api_key)])
async def send_message(
    thread_id: str,
    body: MessageIn,
    request: Request,
    graph: GraphDep,
) -> EventSourceResponse:
    lock = _lock_for(request, thread_id)
    if lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "A run is already in progress on this thread")
    snap = await graph.aget_state(run_config(thread_id, surface="api"))
    if snap.interrupts:
        raise HTTPException(status.HTTP_409_CONFLICT, "Thread is paused; answer it via /resume")
    # First run on a thread needs the full initial state; later runs only
    # append the new message -- the checkpoint already holds the rest.
    payload = {"messages": [HumanMessage(body.text)]} if snap.values else initial_state(body.text)
    return EventSourceResponse(_stream_run(graph, payload, thread_id, lock))


@app.post("/api/threads/{thread_id}/resume", dependencies=[Depends(require_api_key)])
async def resume(
    thread_id: str,
    body: ResumeIn,
    request: Request,
    graph: GraphDep,
) -> EventSourceResponse:
    lock = _lock_for(request, thread_id)
    if lock.locked():
        raise HTTPException(status.HTTP_409_CONFLICT, "A run is already in progress on this thread")
    snap = await graph.aget_state(run_config(thread_id, surface="api"))
    if not snap.interrupts:
        raise HTTPException(status.HTTP_409_CONFLICT, "Nothing to resume on this thread")
    return EventSourceResponse(_stream_run(graph, Command(resume=body.resume), thread_id, lock))
