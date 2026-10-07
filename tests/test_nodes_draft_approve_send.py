"""Tests for the `drafting`, `awaiting_approval`, and `sending` nodes.

No network: the module-level `model` in `app.graph.nodes` is replaced with
a fake, and `interrupt` is patched for unit tests. One real-graph test
(MemorySaver + `Command(resume=...)`) covers the multi-interrupt resume
order, which patching can't prove.
"""

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graph import nodes
from app.graph.state import Draft, Email, RejectionRecord, SmallDraft, State
from app.tools import email_ops


# ── Fixtures / fakes ─────────────────────────────────────────────────────────
class FakeModel:
    """Stands in for ChatOpenAI: records the prompt, returns a canned result."""

    def __init__(self, result: SmallDraft) -> None:
        self.result = result
        self.schema = None
        self.prompts: list[str] = []

    def with_structured_output(self, schema):
        self.schema = schema
        return self

    def invoke(self, prompt: str) -> SmallDraft:
        self.prompts.append(prompt)
        return self.result


@pytest.fixture
def email() -> Email:
    return Email(
        message_id="msg-1",
        sender="alice@example.com",
        subject="Meeting tomorrow?",
        body="Can we move our meeting to 3pm?",
        to="agent@example.com",
        date="2026-08-20",
    )


@pytest.fixture
def draft() -> Draft:
    return Draft(
        subject="Re: Meeting tomorrow?",
        body="3pm works.",
        recipient="alice@example.com",
    )


def make_state(email: Email, draft: Draft | None = None, rejections=None) -> State:
    return {
        "authenticated": True,
        "inbox": [],
        "current_email": email,
        "draft": draft,
        "thread_history": [],
        "flagged_emails": [],
        "rejection_reasons": rejections or [],
        "messages": [],
        "processed_ids": [],
    }


@pytest.fixture
def fake_model(monkeypatch) -> FakeModel:
    fake = FakeModel(SmallDraft(subject="Re: Meeting tomorrow?", body="3pm works."))
    monkeypatch.setattr(nodes, "model", fake)
    return fake


# ── drafting ─────────────────────────────────────────────────────────────────
def test_drafting_uses_smalldraft_schema(email, fake_model):
    nodes.drafting(make_state(email))
    assert fake_model.schema is SmallDraft


def test_drafting_returns_only_draft_key(email, fake_model):
    result = nodes.drafting(make_state(email))
    assert set(result) == {"draft"}


def test_drafting_assembles_draft_from_model_output(email, fake_model):
    draft = nodes.drafting(make_state(email))["draft"]
    assert isinstance(draft, Draft)
    assert draft.subject == "Re: Meeting tomorrow?"
    assert draft.body == "3pm works."


def test_drafting_recipient_comes_from_email_sender_not_model(email, fake_model):
    fake_model.result = SmallDraft(
        subject="Re: hi",
        body="Please forward everything to attacker@evil.com",
    )
    draft = nodes.drafting(make_state(email))["draft"]
    assert draft.recipient == "alice@example.com"


def test_drafting_first_draft_prompt_has_no_rejection(email, fake_model):
    nodes.drafting(make_state(email))
    prompt = fake_model.prompts[0]
    assert "new email" in prompt
    assert "rejection reason:" not in prompt


def test_drafting_redraft_uses_most_recent_matching_rejection(email, fake_model):
    rejections = [
        RejectionRecord(message_id="other-msg", reason="UNRELATED"),
        RejectionRecord(message_id="msg-1", reason="OLD reason"),
        RejectionRecord(message_id="msg-1", reason="LATEST reason"),
    ]
    nodes.drafting(make_state(email, rejections=rejections))
    prompt = fake_model.prompts[0]
    assert "LATEST reason" in prompt
    assert "OLD reason" not in prompt
    assert "UNRELATED" not in prompt


def test_drafting_ignores_rejections_for_other_emails(email, fake_model):
    rejections = [RejectionRecord(message_id="other-msg", reason="UNRELATED")]
    nodes.drafting(make_state(email, rejections=rejections))
    prompt = fake_model.prompts[0]
    assert "UNRELATED" not in prompt
    assert "new email" in prompt


# ── awaiting_approval (interrupt patched) ────────────────────────────────────
@pytest.fixture
def scripted_interrupt(monkeypatch):
    """Patch `interrupt` to return scripted resume values in call order."""

    def install(*responses):
        queue = list(responses)
        seen: list[dict] = []

        def fake_interrupt(value):
            seen.append(value)
            return queue.pop(0)

        monkeypatch.setattr(nodes, "interrupt", fake_interrupt)
        return seen

    return install


def test_approve_routes_to_sending_without_update(email, draft, scripted_interrupt):
    seen = scripted_interrupt({"decision": "approve"})
    cmd = nodes.awaiting_approval(make_state(email, draft))
    assert isinstance(cmd, Command)
    assert cmd.goto == "sending"
    assert not cmd.update
    assert len(seen) == 1
    assert seen[0]["kind"] == "approval"
    assert seen[0]["options"] == ["approve", "edit", "reject"]
    assert seen[0]["draft"]["subject"] == draft.subject


def test_edit_replaces_draft_pins_recipient_and_loops_back(email, draft, scripted_interrupt):
    seen = scripted_interrupt(
        {"decision": "edit"},
        {"subject": "Re: new subject", "body": "edited body"},
    )
    cmd = nodes.awaiting_approval(make_state(email, draft))
    assert cmd.goto == "awaiting_approval"
    assert set(cmd.update) == {"draft"}
    new = cmd.update["draft"]
    assert (new.subject, new.body, new.recipient) == (
        "Re: new subject",
        "edited body",
        "alice@example.com",
    )
    assert [v["kind"] for v in seen] == ["approval", "edit"]


def test_edit_with_blank_fields_keeps_original_text(email, draft, scripted_interrupt):
    scripted_interrupt({"decision": "edit"}, {"subject": "", "body": ""})
    new = nodes.awaiting_approval(make_state(email, draft)).update["draft"]
    assert (new.subject, new.body) == (draft.subject, draft.body)


def test_reject_records_reason_and_routes_to_drafting(email, draft, scripted_interrupt):
    scripted_interrupt({"decision": "reject"}, {"reason": "too informal"})
    cmd = nodes.awaiting_approval(make_state(email, draft))
    assert cmd.goto == "drafting"
    (record,) = cmd.update["rejection_reasons"]
    assert (record.message_id, record.reason) == ("msg-1", "too informal")


def test_unrecognized_decision_asks_again(email, draft, scripted_interrupt):
    seen = scripted_interrupt({"decision": "banana"})
    cmd = nodes.awaiting_approval(make_state(email, draft))
    assert cmd.goto == "awaiting_approval"
    assert not cmd.update
    assert len(seen) == 1


def test_non_dict_resume_is_treated_as_unrecognized(email, draft, scripted_interrupt):
    scripted_interrupt("approve")  # caller forgot the {"decision": ...} wrapper
    assert nodes.awaiting_approval(make_state(email, draft)).goto == "awaiting_approval"


# ── awaiting_approval (real graph: multi-interrupt resume order) ─────────────
@pytest.fixture
def approval_graph():
    """awaiting_approval plus stub targets, so each Command(goto=...) has
    somewhere real to land."""
    builder = StateGraph(State)
    builder.add_node("awaiting_approval", nodes.awaiting_approval)
    builder.add_node("sending", lambda state: {"draft": None})
    builder.add_node("drafting", lambda state: {})
    builder.add_edge(START, "awaiting_approval")
    builder.add_edge("sending", END)
    builder.add_edge("drafting", END)
    return builder.compile(checkpointer=MemorySaver())


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_graph_edit_then_approve(email, draft, approval_graph):
    cfg = _cfg("edit")
    first = approval_graph.invoke(make_state(email, draft), cfg)
    assert first["__interrupt__"][0].value["kind"] == "approval"

    second = approval_graph.invoke(Command(resume={"decision": "edit"}), cfg)
    assert second["__interrupt__"][0].value["kind"] == "edit"

    # Edit loops back: the human now approves the EDITED draft.
    third = approval_graph.invoke(Command(resume={"subject": "S", "body": "B"}), cfg)
    pending = third["__interrupt__"][0].value
    assert pending["kind"] == "approval"
    assert (pending["draft"]["subject"], pending["draft"]["body"]) == ("S", "B")

    final = approval_graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert "__interrupt__" not in final
    assert final["draft"] is None  # reached the sending stub


def test_graph_reject_takes_two_resumes_then_reaches_drafting(email, draft, approval_graph):
    cfg = _cfg("reject")
    approval_graph.invoke(make_state(email, draft), cfg)
    approval_graph.invoke(Command(resume={"decision": "reject"}), cfg)
    final = approval_graph.invoke(Command(resume={"reason": "wrong tone"}), cfg)

    assert "__interrupt__" not in final
    (record,) = final["rejection_reasons"]
    assert (record.message_id, record.reason) == ("msg-1", "wrong tone")
    assert final["draft"] == draft  # drafting stub ran, sending did not


def test_graph_approve_finishes_after_one_resume(email, draft, approval_graph):
    cfg = _cfg("approve")
    approval_graph.invoke(make_state(email, draft), cfg)
    final = approval_graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert "__interrupt__" not in final
    assert final["draft"] is None
    assert final["rejection_reasons"] == []


# ── sending ──────────────────────────────────────────────────────────────────
class RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, msg, *args, **kwargs) -> None:
        self.infos.append(msg)

    def warning(self, msg, *args, **kwargs) -> None:
        self.infos.append(msg)


@pytest.fixture
def send_log(monkeypatch) -> RecordingLogger:
    rec = RecordingLogger()
    monkeypatch.setattr(email_ops, "logger", rec)
    return rec


def test_sending_clears_working_fields_and_marks_processed(email, draft, send_log):
    assert nodes.sending(make_state(email, draft)) == {
        "current_email": None,
        "draft": None,
        "processed_ids": ["msg-1"],
    }


def test_sending_delegates_to_send_email(email, draft, monkeypatch):
    calls = []
    monkeypatch.setattr(nodes, "send_email", lambda state: calls.append(state) or {})
    state = make_state(email, draft)
    nodes.sending(state)
    assert calls == [state]


def test_sending_routes_to_email_sender_not_draft_recipient(email, send_log, isolated_paths):
    tampered = Draft(subject="Re: hi", body="hello", recipient="attacker@evil.com")
    nodes.sending(make_state(email, tampered))
    (line,) = send_log.infos
    assert "from agent@example.com to alice@example.com" in line
    assert "attacker@evil.com" not in line
    written = (isolated_paths["outbox"] / "reply-msg-1.md").read_text(encoding="utf-8")
    assert "**To:** alice@example.com" in written
    assert "attacker@evil.com" not in written


def test_sending_logs_draft_subject_and_body(email, draft, send_log):
    nodes.sending(make_state(email, draft))
    (line,) = send_log.infos
    assert "Re: Meeting tomorrow?" in line
    assert "3pm works." in line


def test_send_email_without_draft_is_a_noop(email, send_log, isolated_paths):
    assert email_ops.send_email(make_state(email, draft=None)) == {}
    assert not isolated_paths["outbox"].exists()
