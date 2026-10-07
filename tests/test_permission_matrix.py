"""Least privilege: which tools each FSM state may give the model."""

from app.tools import TOOL_PERMISSIONS, send_email
from app.tools.email_ops import auth_tool_access, fetch_inbox

FSM_STATES = {
    "unauthenticated",
    "reading",
    "drafting",
    "awaiting_approval",
    "sending",
    "done",
}


def _names(state: str) -> set[str]:
    return {t.name for t in TOOL_PERMISSIONS[state]}


def test_every_fsm_state_is_listed():
    assert set(TOOL_PERMISSIONS) == FSM_STATES


def test_each_tool_state_gets_exactly_one_tool():
    assert _names("unauthenticated") == {"auth_tool_access"}
    assert _names("reading") == {"fetch_inbox"}


def test_states_after_reading_have_no_tools():
    for state in ("drafting", "awaiting_approval", "sending", "done"):
        assert TOOL_PERMISSIONS[state] == []


def test_send_is_never_offered_to_any_model():
    offered = {t.name for tools in TOOL_PERMISSIONS.values() for t in tools}
    assert "send_email" not in offered
    assert not hasattr(send_email, "name")  # plain function, not a @tool


def test_tools_take_no_model_supplied_arguments():
    # Everything they need comes from injected ToolRuntime/state, so a
    # prompt-injected model can't steer them with crafted arguments.
    for tool in (auth_tool_access, fetch_inbox):
        assert tool.tool_call_schema.model_json_schema().get("properties", {}) == {}
