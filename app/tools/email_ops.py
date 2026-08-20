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
from langgraph.types import Command
from pydantic import ValidationError

from app.core import get_logger, settings
from app.graph.state import Email, State

logger = get_logger(__name__)

# Define the data path for the fetch_mails
EMAILS_DATA_PATH = Path(settings.EMAILS_DATA_PATH)

@tool("auth_tool_access", description = "Tool to check if the user is authenticated to access the email triage agent.")
def auth_tool_access(runtime: ToolRuntime) -> Command:
    """Grants access after human approval.

    No input parameters: this tool is gated entirely by
    HumanInTheLoopMiddleware's interrupt_on config, which pauses BEFORE
    this function ever runs and only lets it through on human approval.
    There's nothing for the model to decide or supply here.

    Returns a Command instead of a plain value because `State` is a
    TypedDict, not a Pydantic model — a normal return doesn't mutate
    state on its own. Command(update={...}) is the mechanism for a tool
    to write directly into graph state.

    The `messages` key with a ToolMessage(tool_call_id=...) is required,
    not optional: it links this response back to the specific tool call
    the model made. Without a matching tool_call_id, the model's message
    history has a tool call with no reply, and the conversation breaks.

    Tool name note: interrupt_on matches by this tool's registered name
    — "auth_tool_access" (set explicitly above), not the function name
    `auth_tool`. Keep interrupt_on's key in sync with that string.
    """
    logger.info("User authentication approved via human-in-the-loop.")
    return Command(
        update={
            "authenticated": True,
            "messages": [ToolMessage(content = "User authentication status updated.", tool_call_id = runtime.tool_call_id)]
        }
    )


@tool("fetch_inbox", description = "Tool to fetch the user's inbox emails.")
def fetch_inbox(runtime: ToolRuntime) -> Command: 
    """
    Fetches the user's inbox emails and updates the state.
    """
    email_list = []
    for path in EMAILS_DATA_PATH.glob("*.md"):
        # Fetch the content of the each email file
        text = path.read_text(encoding = "utf-8")
        
        # Regex pattern to extract the email subject, from the email
        #  ── Extract `Subject` ─────────────────────────────────────────────────────────────
        match_subject = re.search(r"\*\*Subject:\*\*\s*(.+)", text)
        subject = match_subject.group(1) if match_subject else None
        #  ──  Extract `Sender` ─────────────────────────────────────────────────────────────
        match_sender = re.search(r"\*\*From:\*\*\s*(.+)", text)
        sender = match_sender.group(1) if match_sender else None
        #  ──  Extract `Date` ─────────────────────────────────────────────────────────────
        match_date = re.search(r"\*\*Date:\*\*\s*(.+)", text)
        date = match_date.group(1) if match_date else None
        #  ──  Extract `To` ─────────────────────────────────────────────────────────────
        match_to = re.search(r"\*\*To:\*\*\s*(.+)", text)
        to = match_to.group(1) if match_to else None            
        #  ──  Extract `Body` after the date ─────────────────────────────────────────────────────────────
        match_body = re.search(r"\*\*Date:\*\*.*?\n(.*)", text, flags=re.DOTALL)
        body = match_body.group(1).strip() if match_body else None
        
        try: 
            email = Email(
                message_id = path.stem,
                sender = sender,
                subject = subject,
                body = body,
                date = date, 
                to = to
            )
            email_list.append(email)
        except ValidationError as exc: 
            logger.warning(f"Skipping invalid email file {path}: {exc}")
            continue
    return Command(
        update = {
            "inbox": email_list,
            "messages": [ToolMessage(content = "Inbox emails fetched and state updated.", tool_call_id = runtime.tool_call_id)]
        }
    )

def send_email(state: State) -> dict:
    """Sends the approved/edited draft as a reply to `current_email`.

    Not a `@tool` -- deliberately. `registry.py`'s `TOOL_PERMISSIONS` never
    lists `send_email` for any of the six FSM states, so no model ever
    decides to call it; it's only ever invoked directly by the `sending`
    node. Without a model-issued tool call there's no `tool_call_id` to
    link a `ToolMessage`/`Command` back to, so this takes plain `state`
    and returns a plain dict, the same shape as `guard`/`drafting`/
    `awaiting_approval` -- not `runtime: ToolRuntime` and `Command`, which
    only apply to tools a model actually invokes (`auth_tool_access`,
    `fetch_inbox`, both bound above).

    Sender/recipient are read from `current_email`, never from `Draft` --
    same anti-injection design as everywhere else in this project:
    `current_email.to` (the address the original mail was addressed to,
    i.e. this agent's own address) becomes the outgoing sender;
    `current_email.sender` (who originally wrote in) becomes the outgoing
    recipient. A model-authored `Draft.recipient` is never trusted for
    where the email actually goes.

    No inbox/email-sending side effect actually wired up yet -- currently
    logs the send. Returns `{}`: nothing about this action needs to be
    written back into graph state.
    """
    subject = state["draft"].subject if state["draft"] else None
    sender = state["current_email"].to if state["current_email"] else None
    # for the reciepient, it does not matter from where to get it, 
    # either from the draft or the current email,
    # as the recipient is the sender of the current email.
    recipient = state["current_email"].sender if state["current_email"] else None
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
    body = state["draft"].body if state["draft"] else None
    
    logger.info(f"Sending email from {sender} to {recipient} on {date} with the body: {body} and subject: {subject}.")
    
    return {}