"""Test env defaults + shared fakes. No test touches the network.

Env: only variables already in the shell win (`setdefault`). `.env` does
not: pydantic-settings ranks real env vars above it, so tests always run
on these dummy values, with LangSmith tracing off. Nothing here touches
the network: `ChatOpenAI` only builds a client at import time.

Fakes: every LLM entry point (`nodes.model`, the two tool-bound models,
`injection.model_with_structured_output`) is swapped for a scripted
stand-in by the `fake_llms` fixture, so whole-graph runs are
deterministic.
"""

import os

_DEFAULTS = {
    "LANGSMITH_API_KEY": "test",
    "LANGSMITH_ENDPOINT": "https://example.invalid",
    "LANGSMITH_TRACING": "false",
    "LANGSMITH_PROJECT": "test",
    "OPENAI_API_KEY": "test",
    "OPENAI_REGURAL_MODEL": "test-regular",
    "OPENAI_MINI_MODEL": "test-mini",
    "EMAILS_DATA_PATH": ".",
    "PII_HMAC_SECRET": "test-secret",
}

for key, value in _DEFAULTS.items():
    os.environ.setdefault(key, value)

# Imports below need the env above to be set first.
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.graph import nodes
from app.graph.state import (
    ClassifierResult,
    SmallDraft,
    SmallFlaggedEmail,
)
from app.guardrails import injection
from app.tools import email_ops


# ── Fake LLMs ────────────────────────────────────────────────────────────────
def tool_call(name: str, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}],
    )


class FakeToolModel:
    """Stands in for `model.bind_tools(...)`: always requests `tool_name`,
    unless the last message is a denial ToolMessage -- then replies in text."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        last = messages[-1]
        if isinstance(last, ToolMessage) and "DENIED" in last.content:
            return AIMessage(content="Mail was not fetched. What else can I do for you?")
        return tool_call(self.tool_name, call_id=f"call_{self.tool_name}_{self.calls}")


class FakeStructuredModel:
    """Stands in for `ChatOpenAI` + `with_structured_output(schema)`."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.draft_count = 0

    def with_structured_output(self, schema):
        self.schema = schema
        return self

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.schema is SmallDraft:
            self.draft_count += 1
            return SmallDraft(subject="Re: reply", body=f"Draft #{self.draft_count}")
        if self.schema is SmallFlaggedEmail:
            return SmallFlaggedEmail(reason="Looks like an attempt to instruct the AI.")
        raise AssertionError(f"unexpected schema {self.schema}")


class FakeClassifier:
    """Stands in for injection's structured classifier: body containing
    'AMBIGUOUS' -> ambiguous, anything else -> clean."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, prompt: str) -> ClassifierResult:
        self.calls += 1
        verdict = "ambiguous" if "AMBIGUOUS" in prompt else "clean"
        return ClassifierResult(injection_type=verdict)


@pytest.fixture
def fake_llms(monkeypatch):
    fakes = {
        "auth": FakeToolModel("auth_tool_access"),
        "fetch": FakeToolModel("fetch_inbox"),
        "model": FakeStructuredModel(),
        "classifier": FakeClassifier(),
    }
    monkeypatch.setattr(nodes, "model_with_auth_tool", fakes["auth"])
    monkeypatch.setattr(nodes, "model_with_fetch_inbox_tool", fakes["fetch"])
    monkeypatch.setattr(nodes, "model", fakes["model"])
    monkeypatch.setattr(injection, "model_with_structured_output", fakes["classifier"])
    return fakes


# ── Filesystem isolation ─────────────────────────────────────────────────────
def write_email(
    folder: Path, stem: str, subject: str, body: str, sender: str = "alice@example.com"
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.md").write_text(
        f"# {stem}\n**Subject:** {subject}\n**From:** {sender}\n"
        f"**To:** agent@example.com\n**Date:** 2026-10-07\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Every test gets its own inbox/outbox; nothing writes into the repo."""
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    monkeypatch.setattr(email_ops, "EMAILS_DATA_PATH", inbox)
    monkeypatch.setattr(email_ops, "OUTBOX_PATH", outbox)
    return {"inbox": inbox, "outbox": outbox}
