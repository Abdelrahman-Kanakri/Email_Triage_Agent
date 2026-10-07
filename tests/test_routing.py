"""Router unit tests: each router is a pure function of state -> next node.

Failure cases first: stale `current_email`, empty inbox, a model reply
with no tool call, a denied auth -- the situations that would loop
forever or draft the wrong email if a router got them wrong.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

from app.graph import build
from app.graph.state import Email
from tests.conftest import tool_call


def _email(i: int = 1) -> Email:
    return Email(
        message_id=f"m{i}",
        sender="a@x.com",
        subject="s",
        body="b",
        to="me@x.com",
        date="d",
    )


# ── START ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("authenticated", "expected"), [(False, "unauthenticated"), (True, "reading")]
)
def test_route_entry(authenticated, expected):
    assert build.route_entry({"authenticated": authenticated}) == expected


def test_route_entry_missing_key_means_not_authenticated():
    assert build.route_entry({}) == "unauthenticated"


# ── tool-decision routers ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("router", "tool_target"),
    [
        (build.route_after_unauthenticated, "auth_tools"),
        (build.route_after_reading, "inbox_tools"),
    ],
)
class TestToolDecisionRouters:
    def test_tool_call_goes_to_tool_node(self, router, tool_target):
        assert router({"messages": [HumanMessage("hi"), tool_call("x")]}) == tool_target

    def test_plain_reply_ends(self, router, tool_target):
        assert router({"messages": [AIMessage("Mail was not fetched.")]}) == END

    def test_empty_history_ends(self, router, tool_target):
        assert router({"messages": []}) == END

    def test_only_last_message_counts(self, router, tool_target):
        # An OLD tool call followed by a text reply must not re-trigger the tool.
        msgs = [
            tool_call("x"),
            ToolMessage("ok", tool_call_id="call_1"),
            AIMessage("done"),
        ]
        assert router({"messages": msgs}) == END


# ── after auth tool ──────────────────────────────────────────────────────────
def test_route_after_auth_granted():
    assert build.route_after_auth({"authenticated": True}) == "reading"


def test_route_after_auth_denied_goes_back_to_model():
    assert build.route_after_auth({"authenticated": False}) == "unauthenticated"


# ── queue routers ────────────────────────────────────────────────────────────
def test_route_next_email_with_mail():
    assert build.route_next_email({"inbox": [_email()]}) == "guard"


@pytest.mark.parametrize("inbox", [[], None])
def test_route_next_email_empty_inbox_ends(inbox):
    assert build.route_next_email({"inbox": inbox}) == END


def test_route_next_email_never_returns_reading():
    # Routing back to `reading` would re-fetch the whole folder forever.
    for state in ({"inbox": []}, {"inbox": [_email()]}):
        assert build.route_next_email(state) != "reading"


def test_route_after_guard_clean_email_drafts():
    assert build.route_after_guard({"current_email": _email(), "inbox": []}) == "drafting"


def test_route_after_guard_flagged_with_more_mail_loops():
    assert build.route_after_guard({"current_email": None, "inbox": [_email(2)]}) == "guard"


def test_route_after_guard_flagged_last_email_ends():
    assert build.route_after_guard({"current_email": None, "inbox": []}) == END
