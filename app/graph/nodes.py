"""Graph node functions for the email triage agent's FSM.

Most nodes are `(state: State) -> dict` -- a plain partial-state update.
The one exception is `awaiting_approval`, which returns
`Command(update=..., goto=...)`: the human's decision is a local variable,
not state, so no router could read it -- the node picks its own next step.

`unauthenticated`/`reading` only let the model DECIDE to call a tool; the
ToolNodes wired in `build.py` (`auth_tools`, `inbox_tools`) execute it.
`guard` is `reading`'s classification step split into its own node,
because a conditional edge can't write state. `done` is routing-only,
mapped to `END` in `build.py`.
"""

from typing import Literal

from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.types import Command, interrupt

from app.core import get_logger, settings
from app.graph.prompts import (
    SYSTEM_PROMPT_AUTH,
    SYSTEM_PROMPT_DRAFT,
    SYSTEM_PROMPT_READING,
    SYSTEM_PROMPT_REASON,
)
from app.graph.state import (
    Draft,
    FlaggedEmail,
    RejectionRecord,
    SmallDraft,
    SmallFlaggedEmail,
    State,
)
from app.guardrails.injection import get_injection_type
from app.tools import TOOL_PERMISSIONS
from app.tools.email_ops import send_email

logger = get_logger(__name__)

# ── Models ─────────────────────────────────────────────────────────────
model = ChatOpenAI(
    model=settings.OPENAI_MINI_MODEL,
    temperature=0.0,
    api_key=settings.OPENAI_API_KEY.get_secret_value(),
)

# Each LLM node only ever sees the tools its FSM state is permitted.
model_with_auth_tool = model.bind_tools(tools=TOOL_PERMISSIONS["unauthenticated"])
model_with_fetch_inbox_tool = model.bind_tools(tools=TOOL_PERMISSIONS["reading"])


# ── Nodes ─────────────────────────────────────────────────────────────
def unauthenticated(state: State) -> dict:
    """Let the model decide whether to request inbox access.

    The system prompt is prepended per call, not stored in `messages`, so
    it never piles up in the checkpointed history. Tool execution (and the
    human approval `interrupt()` inside `auth_tool_access`) happens in the
    `auth_tools` ToolNode downstream, not here.
    """
    result = model_with_auth_tool.invoke([SystemMessage(SYSTEM_PROMPT_AUTH), *state["messages"]])
    logger.info("auth decision", requested_tool=bool(result.tool_calls))
    return {"messages": [result]}


def reading(state: State) -> dict:
    """Let the model decide whether to fetch the inbox (`inbox_tools` executes it)."""
    result = model_with_fetch_inbox_tool.invoke(
        [SystemMessage(SYSTEM_PROMPT_READING), *state["messages"]]
    )
    logger.info("fetch decision", requested_tool=bool(result.tool_calls))
    return {"messages": [result]}


def _flag(state: State, category: str, reason: str) -> dict:
    """Shared return for every non-clean outcome: drop the email from the
    queue, reset `current_email` (so the router can't redraft a stale
    one), record it as flagged and processed."""
    email = state["inbox"][0]
    return {
        "inbox": state["inbox"][1:],
        "current_email": None,
        "flagged_emails": [FlaggedEmail(email=email, reason=reason, category=category)],
        "processed_ids": [email.message_id],
    }


def guard(state: State) -> dict:
    """Classify the next inbox email and either select it or flag it.

    Takes `inbox[0]` (the routers guarantee the inbox is non-empty here)
    and pops it off. Clean -> becomes `current_email` for drafting.
    Injection/ambiguous -> flagged with a model-written reason (narrow
    `SmallFlaggedEmail` schema: the model is only asked for the field it
    doesn't already know). An empty body can't be classified, so it is
    flagged deterministically -- no LLM call needed for that.
    """
    email = state["inbox"][0]

    try:
        classification = get_injection_type(email)
    except ValueError:
        logger.warning("email flagged: empty body", message_id=email.message_id)
        return _flag(state, "empty", "Email body is empty; nothing to classify or reply to.")

    if classification == "clean":
        logger.info("email classified clean", message_id=email.message_id)
        return {"inbox": state["inbox"][1:], "current_email": email}

    logger.warning("email flagged", message_id=email.message_id, category=classification)
    reason = model.with_structured_output(SmallFlaggedEmail).invoke(
        SYSTEM_PROMPT_REASON.format(email=email, category_classified=classification)
    )
    return _flag(state, classification, reason.reason)


def drafting(state: State) -> dict:
    """
    Drafts a reply to `current_email` -- first draft, or a redraft if
    `rejection_reasons` holds a record matching its `message_id` (most
    recent one wins). Calls the model with `SmallDraft` (subject + body
    only, no `recipient`) via `with_structured_output`; the real `Draft`
    is assembled after, with `recipient` taken from `current_email.sender`,
    never from the model -- same anti-injection design as `send_email`.
    No tool bound (`registry.py`: `"drafting": []`), so nothing to decide;
    returns `draft` only, `messages` untouched.
    """
    # Bind the model with the structured output schema for drafting
    model_with_structured_output = model.with_structured_output(SmallDraft)

    # Extract the current email and any rejection reasons from the state
    current_email = state["current_email"]
    # List comprehension to filter rejection reasons for the current email based on the message_id
    rejection_reasons = [
        record
        for record in state["rejection_reasons"]
        if record.message_id == current_email.message_id
    ]

    # Branching logic based on whether there are rejection reasons for the current email
    if rejection_reasons:
        logger.info(f"Redrafting reply for {current_email.message_id} after rejection.")
        draft_context = f"Please draft a professional reply to the email above, taking into account the following rejection reason: {rejection_reasons[-1].reason}"
    else:
        logger.info(f"Drafting reply for {current_email.message_id}.")
        draft_context = "This is a new email, there is no a rejection reason for it, so please draft a professional reply to the email above."

    result = model_with_structured_output.invoke(
        SYSTEM_PROMPT_DRAFT.format(email=current_email, draft_context=draft_context)
    )

    return {
        "draft": Draft(subject=result.subject, body=result.body, recipient=current_email.sender)
    }


def awaiting_approval(
    state: State,
) -> Command[Literal["sending", "drafting", "awaiting_approval"]]:
    """Pause for the human's verdict on the draft, then route via `Command`.

    Routes itself instead of using a router: a router can only read state,
    and the human's choice lives in a local variable. The `Literal[...]`
    return type is what tells LangGraph which edges this node can take.

    - approve -> `sending`
    - edit    -> self-loop with the human's literal text (no AI involved)
    - reject  -> `drafting` with the reason recorded for the redraft
    - unknown -> self-loop, ask again

    Resume contract (what the caller passes to `Command(resume=...)`):
    first `{"decision": "approve"|"edit"|"reject"}`, then for edit
    `{"subject": ..., "body": ...}`, for reject `{"reason": ...}`.
    Multiple `interrupt()` calls in one node are matched by call order.
    """
    current_email = state["current_email"]
    draft = state["draft"]
    email_view = {
        "message_id": current_email.message_id,
        "sender": current_email.sender,
        "subject": current_email.subject,
        "body": current_email.body,
    }
    draft_view = {
        "subject": draft.subject,
        "body": draft.body,
        "recipient": draft.recipient,
    }

    human_decision = interrupt(
        {
            "kind": "approval",
            "question": f"Approve, edit, or reject the reply to {current_email.sender}?",
            "options": ["approve", "edit", "reject"],
            "email": email_view,
            "draft": draft_view,
        }
    )
    decision = human_decision.get("decision") if isinstance(human_decision, dict) else None

    if decision == "approve":
        logger.info("draft approved", message_id=current_email.message_id)
        return Command(goto="sending")

    if decision == "edit":
        human_edit = interrupt(
            {
                "kind": "edit",
                "question": "Provide the edited subject and body.",
                "options": ["submit"],
                "email": email_view,
                "draft": draft_view,
            }
        )
        logger.info("draft edited by human", message_id=current_email.message_id)
        return Command(
            update={
                "draft": Draft(
                    subject=human_edit.get("subject") or draft.subject,
                    body=human_edit.get("body") or draft.body,
                    recipient=current_email.sender,
                )
            },
            goto="awaiting_approval",
        )

    if decision == "reject":
        rejection = interrupt(
            {
                "kind": "reject_reason",
                "question": "Why was the draft rejected? The reason is used for the redraft.",
                "options": ["submit"],
                "email": email_view,
                "draft": draft_view,
            }
        )
        logger.info("draft rejected by human", message_id=current_email.message_id)
        return Command(
            update={
                "rejection_reasons": [
                    RejectionRecord(
                        message_id=current_email.message_id,
                        reason=rejection.get("reason") or "No reason given.",
                    )
                ]
            },
            goto="drafting",
        )

    logger.warning(
        "unrecognized human decision",
        message_id=current_email.message_id,
        decision=str(decision),
    )
    return Command(goto="awaiting_approval")


def sending(state: State) -> dict:
    """Send the approved/edited draft. No LLM step, no tool binding.

    `TOOL_PERMISSIONS["sending"]` is `[]` -- `send_email` is never offered
    to a model. Clears `current_email`/`draft` so nothing stale leaks into
    the next email, and records the id so a later fetch skips it.
    """
    send_email(state)
    return {
        "current_email": None,
        "draft": None,
        "processed_ids": [state["current_email"].message_id],
    }
