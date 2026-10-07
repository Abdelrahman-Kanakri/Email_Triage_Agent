"""The auth gate: `auth_tool_access` pauses via `interrupt()` inside the tool.

Run through a real `ToolNode` + `MemorySaver`, because that is the only
place a `ToolRuntime` (with its `tool_call_id`) exists -- and proves the
interrupt bubbles out of the ToolNode instead of being swallowed as a
tool error.
"""

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from app.graph.state import State
from app.tools import TOOL_PERMISSIONS
from tests.conftest import tool_call


@pytest.fixture
def gate():
    builder = StateGraph(State)
    builder.add_node("auth_tools", ToolNode(TOOL_PERMISSIONS["unauthenticated"]))
    builder.add_edge(START, "auth_tools")
    builder.add_edge("auth_tools", END)
    return builder.compile(checkpointer=MemorySaver())


def _start(gate, thread: str):
    cfg = {"configurable": {"thread_id": thread}}
    out = gate.invoke(
        {
            "messages": [tool_call("auth_tool_access", "call_auth")],
            "authenticated": False,
        },
        cfg,
    )
    return cfg, out


def test_tool_pauses_before_granting(gate):
    _, out = _start(gate, "pause")
    (intr,) = out["__interrupt__"]
    assert intr.value["kind"] == "auth"
    assert intr.value["options"] == ["approve", "deny"]
    assert out["authenticated"] is False


def test_approve_sets_authenticated_and_replies_to_the_tool_call(gate):
    cfg, _ = _start(gate, "approve")
    out = gate.invoke(Command(resume={"decision": "approve"}), cfg)
    assert out["authenticated"] is True
    reply = out["messages"][-1]
    assert isinstance(reply, ToolMessage)
    assert reply.tool_call_id == "call_auth"


@pytest.mark.parametrize(
    "resume",
    [{"decision": "deny"}, {"decision": "APPROVE!"}, {"other": "x"}, "approve"],
)
def test_anything_but_explicit_approve_is_a_denial(gate, resume):
    # Fail closed: typos, wrong keys, and wrong shapes never grant access.
    # (An EMPTY dict is not a resume at all to LangGraph -- the API rejects it.)
    cfg, _ = _start(gate, f"deny-{resume!r}")
    out = gate.invoke(Command(resume=resume), cfg)
    assert out["authenticated"] is False
    reply = out["messages"][-1]
    assert reply.tool_call_id == "call_auth"  # the tool call still gets an answer
    assert "DENIED" in reply.content
