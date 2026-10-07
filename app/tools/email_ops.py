"""Tools available to the triage agent.

ToolRuntime: an injected parameter, hidden from the model's tool schema,
carrying things the model shouldn't supply itself — state, the current
tool_call_id, config, store, a stream writer. Add it to a tool's
signature whenever that tool needs to read/write graph state, needs its
own tool_call_id (any Command-returning tool does), or must pull a value
from state instead of trusting a model-supplied argument — e.g. send's
recipient has to come from state, not the model, per the injection
threat model. Skip it for tools that are pure functions of their
model-given arguments alone.
"""

import re
from datetime import datetime
from pathlib import Path

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from app.core import get_logger, settings
from app.graph.state import Email, State

logger = get_logger(__name__)

# Paths are module attributes (not read inside the functions) so tests can
# monkeypatch them to a tmp_path.
EMAILS_DATA_PATH = Path(settings.EMAILS_DATA_PATH)
OUTBOX_PATH = Path(settings.OUTBOX_PATH)


@tool("auth_tool_access", description="Ask the human to grant inbox access.")
def auth_tool_access(runtime: ToolRuntime) -> Command:
    """Pause for human approval; write `authenticated` either way.

    The `interrupt()` lives inside the tool because `HumanInTheLoopMiddleware`
    only attaches to `create_agent`, not a raw `StateGraph`. A denial still
    returns a ToolMessage -- every tool call needs a reply linked by
    `tool_call_id`, or the message history breaks.

    Resume contract: `Command(resume={"decision": "approve" | "deny"})`.
    """
    decision = interrupt(
        {
            "kind": "auth",
            "question": "Allow the agent to access your inbox?",
            "options": ["approve", "deny"],
        }
    )
    approved = isinstance(decision, dict) and decision.get("decision") == "approve"
    if approved:
        logger.info("inbox access granted by human")
        return Command(
            update={
                "authenticated": True,
                "messages": [
                    ToolMessage(content="Access granted.", tool_call_id=runtime.tool_call_id)
                ],
            }
        )
    logger.info("inbox access denied by human")
    return Command(
        update={
            "authenticated": False,
            "messages": [
                ToolMessage(
                    content="Access DENIED by the user. Do not call this tool again. "
                    "Tell the user the mail was not fetched and ask what else they need.",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )


def parse_email_file(path: Path) -> Email | None:
    """Parse one markdown email file; `None` if a required header is missing.

    Pure function (no tool, no state) so the parsing is unit-testable on
    its own. Body = everything after the `**Date:**` line.
    """
    text = path.read_text(encoding="utf-8")

    def header(name: str) -> str | None:
        match = re.search(rf"\*\*{name}:\*\*\s*(.+)", text)
        return match.group(1).strip() if match else None

    match_body = re.search(r"\*\*Date:\*\*.*?\n(.*)", text, flags=re.DOTALL)
    try:
        return Email(
            message_id=path.stem,
            sender=header("From"),
            subject=header("Subject"),
            body=match_body.group(1).strip() if match_body else None,
            date=header("Date"),
            to=header("To"),
        )
    except ValidationError as exc:
        logger.warning("skipping invalid email file", file=path.name, errors=exc.error_count())
        return None


@tool("fetch_inbox", description="Fetch the user's unprocessed inbox emails.")
def fetch_inbox(runtime: ToolRuntime) -> Command:
    """Load every `*.md` email not yet processed in this thread into `inbox`.

    Sorted by filename so runs are deterministic (glob order isn't).
    Emails whose id is in `processed_ids` (already sent or flagged on
    this thread) are skipped.
    """
    processed = set(runtime.state.get("processed_ids") or [])
    emails = [
        email
        for path in sorted(EMAILS_DATA_PATH.glob("*.md"))
        if (email := parse_email_file(path)) is not None and email.message_id not in processed
    ]
    logger.info("inbox fetched", count=len(emails), skipped=len(processed))
    return Command(
        update={
            "inbox": emails,
            "messages": [
                ToolMessage(
                    content=f"Fetched {len(emails)} unprocessed email(s).",
                    tool_call_id=runtime.tool_call_id,
                )
            ],
        }
    )


def send_email(state: State) -> dict:
    """Send the approved/edited draft as a reply to `current_email`.

    Not a `@tool`: no FSM state's `TOOL_PERMISSIONS` lists it, so no model
    can ever decide to call it -- only the `sending` node does, after an
    explicit human approval.

    Sender/recipient come from `current_email`, never from `Draft` --
    anti-injection: a model-authored `Draft.recipient` is never trusted for
    where mail goes. `current_email.to` (this agent's address) becomes the
    outgoing sender; `current_email.sender` becomes the recipient.

    "Sending" = writing the reply to `OUTBOX_PATH` as markdown (a stand-in
    for an SMTP/Gmail call), so the result is inspectable. Returns `{}`.
    """
    email = state["current_email"]
    draft = state["draft"]
    if email is None or draft is None:
        logger.warning("send skipped: no current email or draft")
        return {}

    sender, recipient = email.to, email.sender
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
    logger.info(
        f"Sending email from {sender} to {recipient} on {date} "
        f"with the body: {draft.body} and subject: {draft.subject}."
    )

    OUTBOX_PATH.mkdir(parents=True, exist_ok=True)
    (OUTBOX_PATH / f"reply-{email.message_id}.md").write_text(
        f"**Subject:** {draft.subject}\n**From:** {sender}\n**To:** {recipient}\n"
        f"**Date:** {date}\n**In-Reply-To:** {email.message_id}\n\n{draft.body}\n",
        encoding="utf-8",
    )
    return {}
