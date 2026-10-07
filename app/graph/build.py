"""Wires the FSM nodes, two ToolNodes, and the routers into the triage graph.

unauthenticated/reading only let the model DECIDE to call a tool; the
ToolNodes (auth_tools, inbox_tools) execute it and inject ToolRuntime.
awaiting_approval routes itself via Command(goto=...), so it has no
outgoing edge here -- its `Literal[...]` return type declares them.

Edge map (also in the Excalidraw diagram, "Phase 2- Step 5"):

| from              | reads                   | to                                |
| ----------------- | ----------------------- | --------------------------------- |
| START             | authenticated           | unauthenticated / reading         |
| unauthenticated   | messages[-1].tool_calls | auth_tools / END                  |
| auth_tools        | authenticated           | reading / unauthenticated         |
| reading           | messages[-1].tool_calls | inbox_tools / END                 |
| inbox_tools       | inbox                   | guard / END                       |
| guard             | current_email, inbox    | drafting / guard / END            |
| drafting          | -                       | awaiting_approval                 |
| awaiting_approval | human decision          | sending / drafting / itself       |
| sending           | inbox                   | guard / END                       |
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import aiosqlite
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from app.graph.nodes import (
    awaiting_approval,
    drafting,
    guard,
    reading,
    sending,
    unauthenticated,
)
from app.graph.state import State
from app.tools import TOOL_PERMISSIONS

# Checkpoints store our Pydantic models. Allowlisting them explicitly means
# the checkpointer will refuse to deserialize any *other* arbitrary class --
# a checkpoint DB someone tampered with can't make us instantiate it.
SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[
        ("app.graph.state", name) for name in ("Email", "Draft", "FlaggedEmail", "RejectionRecord")
    ]
)


# ── Routers ─────────────────────────────────────────────────────────────
def _last_message_has_tool_calls(state: State) -> bool:
    messages = state.get("messages", [])
    return bool(messages) and bool(getattr(messages[-1], "tool_calls", None))


def route_entry(state: State) -> Literal["unauthenticated", "reading"]:
    """Auth is one-time per thread: skip it on a resumed, already-granted thread."""
    return "reading" if state.get("authenticated") else "unauthenticated"


def route_after_unauthenticated(state: State) -> str:
    """Model asked for access -> run the tool; plain reply (incl. denial msg) -> stop."""
    return "auth_tools" if _last_message_has_tool_calls(state) else END


def route_after_auth(state: State) -> Literal["reading", "unauthenticated"]:
    """Granted -> read mail; denied -> back to the model so it can tell the user."""
    return "reading" if state.get("authenticated") else "unauthenticated"


def route_after_reading(state: State) -> str:
    """Model asked to fetch -> run the tool; plain reply -> stop."""
    return "inbox_tools" if _last_message_has_tool_calls(state) else END


def route_next_email(state: State) -> str:
    """Used after fetch and after send. Never routes back to `reading`:
    fetch_inbox re-reads the inbox folder, which would loop forever."""
    return "guard" if state.get("inbox") else END


def route_after_guard(state: State) -> str:
    """guard sets current_email only on clean; flagged branches reset it to None."""
    if state.get("current_email") is not None:
        return "drafting"
    return "guard" if state.get("inbox") else END


# ── Graph ─────────────────────────────────────────────────────────────
def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """Compile the triage graph.

    `interrupt()` requires a checkpointer: the paused run's state has to be
    saved somewhere so `Command(resume=...)` can continue it. Defaults to an
    in-memory `MemorySaver` (CLI/tests, lost on exit); the API passes a
    persistent SQLite saver via `sqlite_checkpointer()`.
    """
    builder = StateGraph(State)

    builder.add_node("unauthenticated", unauthenticated)
    builder.add_node("auth_tools", ToolNode(TOOL_PERMISSIONS["unauthenticated"]))
    builder.add_node("reading", reading)
    builder.add_node("inbox_tools", ToolNode(TOOL_PERMISSIONS["reading"]))
    builder.add_node("guard", guard)
    builder.add_node("drafting", drafting)
    builder.add_node("awaiting_approval", awaiting_approval)
    builder.add_node("sending", sending)

    builder.add_conditional_edges(START, route_entry, ["unauthenticated", "reading"])
    builder.add_conditional_edges(
        "unauthenticated", route_after_unauthenticated, ["auth_tools", END]
    )
    builder.add_conditional_edges("auth_tools", route_after_auth, ["reading", "unauthenticated"])
    builder.add_conditional_edges("reading", route_after_reading, ["inbox_tools", END])
    builder.add_conditional_edges("inbox_tools", route_next_email, ["guard", END])
    builder.add_conditional_edges("guard", route_after_guard, ["drafting", "guard", END])
    builder.add_edge("drafting", "awaiting_approval")
    builder.add_conditional_edges("sending", route_next_email, ["guard", END])

    return builder.compile(checkpointer=checkpointer or MemorySaver(serde=SERDE))


@asynccontextmanager
async def sqlite_checkpointer(db_path: str) -> AsyncIterator[AsyncSqliteSaver]:
    """Persistent async checkpointer for the API (survives restarts).

    SQLite, not Postgres: single-process, single-user deployment; one file
    on a mounted volume is enough. Swap for `AsyncPostgresSaver` if the API
    ever runs as several replicas.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as conn:
        yield AsyncSqliteSaver(conn, serde=SERDE)


# ── Run helpers (shared by CLI, API, tests) ────────────────────────────
def initial_state(user_text: str) -> dict:
    """Full input for a thread's FIRST run.

    Every key is set because nodes read `state["current_email"]` etc.
    directly -- a missing key would be a `KeyError`, not a `None`. Later
    runs on the same thread pass only `{"messages": [...]}`; the
    checkpoint already holds the rest.
    """
    return {
        "messages": [HumanMessage(user_text)],
        "authenticated": False,
        "inbox": [],
        "current_email": None,
        "draft": None,
        "thread_history": [],
        "flagged_emails": [],
        "rejection_reasons": [],
        "processed_ids": [],
    }


def run_config(thread_id: str, surface: str) -> RunnableConfig:
    """Config for one graph call.

    `thread_id` selects the checkpoint. `run_name`/`tags`/`metadata` are
    what LangSmith shows -- filter traces by thread or by surface
    (cli/api) without digging through every run.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": "email-triage",
        "tags": [surface],
        "metadata": {"thread_id": thread_id, "surface": surface},
        "recursion_limit": 200,
    }
