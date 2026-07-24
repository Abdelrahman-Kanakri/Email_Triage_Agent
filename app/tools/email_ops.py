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
from app.graph.state import Email

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

@tool("send_email", description = "Tool to send an email from the user's inbox.")
def send_email(runtime: ToolRuntime) -> Command: 
    """
    Sends an email from the user's inbox and updates the state.
    """
    subject = runtime.state["draft"].subject if runtime.state["draft"] else None
    sender = runtime.state["current_email"].to if runtime.state["current_email"] else None
    # for the reciepient, it does not matter from where to get it, 
    # either from the draft or the current email,
    # as the recipient is the sender of the current email.
    recipient = runtime.state["current_email"].sender if runtime.state["current_email"] else None
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
    body = runtime.state["draft"].body if runtime.state["draft"] else None
    
    logger.info(f"Sending email from {sender} to {recipient} on {date} with the body: {body} and subject: {subject}.")
    
    return Command(
        update = {
            "messages": [ToolMessage(content = f"Email sent from {sender} to {recipient} on {date} with the body: {body} and subject: {subject}.", tool_call_id = runtime.tool_call_id)]
        }
    )
