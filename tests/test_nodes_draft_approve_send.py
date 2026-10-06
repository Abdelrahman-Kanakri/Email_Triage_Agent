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


def test_approve_returns_empty_update(email, draft, scripted_interrupt):
    seen = scripted_interrupt({"decision": "approve"})
    assert nodes.awaiting_approval(make_state(email, draft)) == {}
    assert len(seen) == 1
    assert seen[0]["options"] == ["approve", "edit", "reject"]


def test_edit_replaces_draft_and_pins_recipient(email, draft, scripted_interrupt):
    seen = scripted_interrupt(
        {"decision": "edit"},
        {"subject": "Re: new subject", "body": "edited body"},
    )
    result = nodes.awaiting_approval(make_state(email, draft))
    assert set(result) == {"draft"}
    assert result["draft"].subject == "Re: new subject"
    assert result["draft"].body == "edited body"
    assert result["draft"].recipient == "alice@example.com"
    assert len(seen) == 2
    assert seen[1]["options"] == ["submit"]


def test_reject_records_reason_against_message_id(email, draft, scripted_interrupt):
    scripted_interrupt({"decision": "reject"}, {"reason": "too informal"})
    result = nodes.awaiting_approval(make_state(email, draft))
    assert set(result) == {"rejection_reasons"}
    (record,) = result["rejection_reasons"]
    assert record.message_id == "msg-1"
    assert record.reason == "too informal"


def test_unrecognized_decision_returns_empty_update(email, draft, scripted_interrupt):
    seen = scripted_interrupt({"decision": "banana"})
    assert nodes.awaiting_approval(make_state(email, draft)) == {}
    assert len(seen) == 1


# ── awaiting_approval (real graph: multi-interrupt resume order) ─────────────
@pytest.fixture
def approval_graph():
    builder = StateGraph(State)
    builder.add_node("awaiting_approval", nodes.awaiting_approval)
    builder.add_edge(START, "awaiting_approval")
    builder.add_edge("awaiting_approval", END)
    return builder.compile(checkpointer=MemorySaver())


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_graph_edit_takes_two_resumes_in_order(email, draft, approval_graph):
    cfg = _cfg("edit")
    first = approval_graph.invoke(make_state(email, draft), cfg)
    assert first["__interrupt__"][0].value["options"] == ["approve", "edit", "reject"]

    second = approval_graph.invoke(Command(resume={"decision": "edit"}), cfg)
    assert second["__interrupt__"][0].value["options"] == ["submit"]

    final = approval_graph.invoke(
        Command(resume={"subject": "S", "body": "B"}), cfg
    )
    assert "__interrupt__" not in final
    assert final["draft"].subject == "S"
    assert final["draft"].body == "B"
    assert final["draft"].recipient == "alice@example.com"


def test_graph_reject_takes_two_resumes_in_order(email, draft, approval_graph):
    cfg = _cfg("reject")
    approval_graph.invoke(make_state(email, draft), cfg)
    approval_graph.invoke(Command(resume={"decision": "reject"}), cfg)
    final = approval_graph.invoke(Command(resume={"reason": "wrong tone"}), cfg)

    assert "__interrupt__" not in final
    (record,) = final["rejection_reasons"]
    assert record.message_id == "msg-1"
    assert record.reason == "wrong tone"


def test_graph_approve_finishes_after_one_resume(email, draft, approval_graph):
    cfg = _cfg("approve")
    approval_graph.invoke(make_state(email, draft), cfg)
    final = approval_graph.invoke(Command(resume={"decision": "approve"}), cfg)
    assert "__interrupt__" not in final
    assert final["draft"] == draft
    assert final["rejection_reasons"] == []


# ── sending ──────────────────────────────────────────────────────────────────
class RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []

    def info(self, msg, *args, **kwargs) -> None:
        self.infos.append(msg)


@pytest.fixture
def send_log(monkeypatch) -> RecordingLogger:
    rec = RecordingLogger()
    monkeypatch.setattr(email_ops, "logger", rec)
    return rec


def test_sending_returns_empty_update(email, draft, send_log):
    assert nodes.sending(make_state(email, draft)) == {}


def test_sending_delegates_to_send_email(email, draft, monkeypatch):
    calls = []
    monkeypatch.setattr(nodes, "send_email", lambda state: calls.append(state) or {})
    state = make_state(email, draft)
    nodes.sending(state)
    assert calls == [state]


def test_sending_routes_to_email_sender_not_draft_recipient(email, send_log):
    tampered = Draft(
        subject="Re: hi", body="hello", recipient="attacker@evil.com"
    )
    nodes.sending(make_state(email, tampered))
    (line,) = send_log.infos
    assert "from agent@example.com to alice@example.com" in line
    assert "attacker@evil.com" not in line


def test_sending_logs_draft_subject_and_body(email, draft, send_log):
    nodes.sending(make_state(email, draft))
    (line,) = send_log.infos
    assert "Re: Meeting tomorrow?" in line
    assert "3pm works." in line
