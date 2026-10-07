"""Inbox parsing + the fetch tool's dedup (via a real ToolNode)."""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.graph.state import State
from app.tools import TOOL_PERMISSIONS
from app.tools.email_ops import parse_email_file
from tests.conftest import tool_call, write_email


def test_parse_email_file_reads_headers_and_body(isolated_paths):
    write_email(isolated_paths["inbox"], "e1", "Hello", "Line one\nLine two")
    email = parse_email_file(isolated_paths["inbox"] / "e1.md")
    assert email.message_id == "e1"
    assert (email.subject, email.sender, email.to) == (
        "Hello",
        "alice@example.com",
        "agent@example.com",
    )
    assert email.body == "Line one\nLine two"


def test_parse_email_file_missing_header_returns_none(isolated_paths):
    path = isolated_paths["inbox"] / "broken.md"
    path.write_text("**Subject:** no sender here\n\nbody", encoding="utf-8")
    assert parse_email_file(path) is None


def _run_fetch(processed: list[str]) -> dict:
    builder = StateGraph(State)
    builder.add_node("inbox_tools", ToolNode(TOOL_PERMISSIONS["reading"]))
    builder.add_edge(START, "inbox_tools")
    builder.add_edge("inbox_tools", END)
    graph = builder.compile(checkpointer=MemorySaver())
    return graph.invoke(
        {
            "messages": [tool_call("fetch_inbox")],
            "processed_ids": processed,
            "inbox": [],
        },
        {"configurable": {"thread_id": "t"}},
    )


def test_fetch_is_sorted_skips_invalid_and_processed(isolated_paths):
    inbox = isolated_paths["inbox"]
    write_email(inbox, "b", "second", "body b")
    write_email(inbox, "a", "first", "body a")
    write_email(inbox, "c", "done already", "body c")
    (inbox / "zz-broken.md").write_text("no headers", encoding="utf-8")

    out = _run_fetch(processed=["c"])
    assert [e.message_id for e in out["inbox"]] == ["a", "b"]
    assert out["messages"][-1].content == "Fetched 2 unprocessed email(s)."


def test_fetch_empty_folder(isolated_paths):
    assert _run_fetch(processed=[])["inbox"] == []
