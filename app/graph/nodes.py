"""Graph node functions for the email triage agent's six-state FSM.

Each function is `(state: State) -> dict` -- a plain partial-state-update
return, never `Command`. `Command(update=...)` exists specifically to link
a tool's response back to a model-issued `tool_call_id`
(`app/tools/email_ops.py`); nothing in this module is invoked by a model
deciding to call a tool, so nothing here needs it -- including
`unauthenticated`/`reading`, whose *own* returns are just the model's
`AIMessage` turn, not a tool's reply.

Two of the six locked FSM states have no function here on purpose:
`guard` isn't one of the six -- it's `reading`'s Step B split into its own
node (classify one email, pick `current_email` or flag it) because a
conditional edge can't write state. `done` is routing-only, mapped
straight to `END` in `build.py`, nothing to execute.
"""
from langchain_mistralai import ChatMistralAI
from langgraph.types import interrupt

from app.core import get_logger, settings
from app.graph.prompts import SYSTEM_PROMPT_DRAFT, SYSTEM_PROMPT_REASON
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

# Initialize logger for this module
logger = get_logger(__name__)

# -── Model with Tools ─────────────────────────────────────────────────────────────
# Define the LLM for tool access with the SchemaModel output parser
model = ChatMistralAI(
    model = settings.MEDIUM_MODEL_NAME,
    temperature = 0.0,
    api_key = settings.MISTRAL_API_KEY,
)

# Bind the model with the tools authorized for the node's state.
model_with_auth_tool = model.bind_tools(
    tools = TOOL_PERMISSIONS["unauthenticated"],
    )    

model_with_fetch_inbox_tool = model.bind_tools(
    tools = TOOL_PERMISSIONS["reading"],
)

# -── Nodes ─────────────────────────────────────────────────────────────
def unauthenticated(state: State) -> dict:
    """
    Calls the model with `auth_tool_access` bound; the model decides
    whether to call it. Returns the response message only -- tool
    execution and the human-approval interrupt happen downstream via
    `HumanInTheLoopMiddleware`, not inside this node.
    """
    result = model_with_auth_tool.invoke(state["messages"])
    if result.tool_calls:
        logger.info("Model requested authentication tool call.")
    else:
        logger.info("Model responded without requesting authentication.")
    return {"messages": [result]}


def reading(state: State) -> dict: 
    """
    Calls the model with `fetch_inbox` bound; the model decides whether
    to call it. Returns the response message only -- tool execution of the fetch_inbox tool 
    and updated inbox state.
    """
    result = model_with_fetch_inbox_tool.invoke(state["messages"])
    if result.tool_calls:
        logger.info("Model requested inbox fetch.")
    else:
        logger.info("Model responded without requesting an inbox fetch.")
    return {"messages": [result]}

def guard(state: State) -> dict:
    """
    ...
    """
    email = state["inbox"][0]
    full_inbox = state["inbox"][1:]
    classification_result = get_injection_type(email)
    # with_structured_output(Schema).invoke(...) returns a validated instance
    # of Schema directly -- not an AIMessage. A plain model.invoke(...) (no
    # wrapper) returns an AIMessage instead, whose text lives in .content.
    # SmallFlaggedEmail is the narrow schema for this call (just `reason` --
    # `email`/`category` are already known, so the model isn't asked to
    # invent them). That's why reason_of_injection.reason /
    # reason_of_ambiguity.reason below are direct attribute reads, no
    # .content unwrapping -- same pattern as injection.py's
    # injection_result.injection_type.
    model_with_structured_output = model.with_structured_output(SmallFlaggedEmail)
    # Branch based on the classification result
    # ── Clean Branch ─────────────────────────────────────────────────────────────
    if classification_result == "clean":
        logger.info(f"Email {email.message_id} classified as clean.")
        current_email = email
        return {
                "inbox": full_inbox,
                "current_email": current_email,
            }
    # ── Injection Branch ─────────────────────────────────────────────────────────────
    elif classification_result == "injection":
        logger.warning(f"Email {email.message_id} classified as injection attempt.")
        # Get the reason for the injection from the model
        reason_of_injection = model_with_structured_output.invoke(
            SYSTEM_PROMPT_REASON.format(email=email,
                                        category_classified=classification_result)
            )
        # Narrow schema (SmallFlaggedEmail) only for what's asked of the
        # model above; full schema (FlaggedEmail) for what's actually stored
        # here -- email/category are already-known values, not re-requested
        # from the model, so only .reason comes from reason_of_injection.
        return {
            "inbox": full_inbox,
            "flagged_emails": [
                                FlaggedEmail(
                                    email=email,
                                    reason=reason_of_injection.reason,
                                    category=classification_result
                                )
                            ],
        }
    # ── Ambiguous Branch ─────────────────────────────────────────────────────────────
    else: 
        logger.warning(f"Email {email.message_id} classified as ambiguous.")
        # Get the reason for the ambiguity from the model
        reason_of_ambiguity = model_with_structured_output.invoke(
            SYSTEM_PROMPT_REASON.format(email=email,
                                        category_classified=classification_result)
            )
        # Same narrow-vs-full-schema reasoning as the injection branch above.
        return {
            "inbox": full_inbox,
            "flagged_emails": [
                                FlaggedEmail(
                                    email=email,
                                    reason=reason_of_ambiguity.reason,
                                    category=classification_result
                                )
                            ],
        }

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
    rejection_reasons = [record for record in state["rejection_reasons"] if record.message_id == current_email.message_id]
    
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
        "draft": Draft(
            subject = result.subject,
            body = result.body,
            recipient = current_email.sender   
        )
    }

def awaiting_approval(state: State) -> dict:
    """
    This node represents the state where the system is awaiting human approval 
    for the drafted email. It does not perform any model invocation or tool execution.
    where the draft if accepted goes to the sending node, 
    and if rejected, goes back to the drafting node with the rejection reason
    to redraft again using the rejection reason.
    """
    current_email = state["current_email"]
    draft = state["draft"]
    
    # push to human the email, and the draft created for approval, and wait for the human to approve or reject the draft
    human_decision = interrupt(
        value = 
        {
            "question": f"""
            Here its the email you received:
            \n\n{current_email.subject}\n\n
            {current_email.body}\n\n\n
            Here its the draft you created:
            \n\n{draft.subject}\n\n
            {draft.body}\n\n\n
            Please approve or reject the draft. If you reject it, please provide a reason for the rejection. 
            If you approve it, the draft will be sent to the recipient {current_email.sender}.
            """,
        "options": ["approve", "edit", "reject"],
        }
    )

    if human_decision["decision"] == "approve":
        logger.info(f"Draft approved by human for {current_email.message_id}.")
        return {} # Nothing to return since its a approval.
    elif human_decision["decision"] == "edit":
        human_edit = interrupt(
            value = 
            {
                "question": f"""
                Here its the draft you created:
                \n\n{draft.subject}\n\n
                {draft.body}\n\n\n
                Please provide your edits to the draft. The edited draft will be sent to the recipient {current_email.sender}.
                """,
            "options": ["submit"],
            }
        )
        logger.info(f"Draft edited by human for {current_email.message_id}.")
        return {
            "draft": Draft(
                subject=human_edit["subject"],
                body=human_edit["body"],
                recipient=current_email.sender
            )
        }
    elif human_decision["decision"] == "reject":
        rejection_reason = interrupt(
            value = 
            {
                "question": f"""
                Here its the draft you created:
                \n\n{draft.subject}\n\n
                {draft.body}\n\n\n
                Please provide your reason for rejecting the draft. The rejection reason will be used to redraft the email.
                """,
            "options": ["submit"],
            }
        )
        logger.info(f"Draft rejected by human for {current_email.message_id}.")
        return {
            "rejection_reasons":[
                RejectionRecord(
                    message_id=current_email.message_id,
                    reason=rejection_reason["reason"]
                )
            ]
        }
    else:
        logger.warning(f"Unrecognized human decision for {current_email.message_id}: {human_decision}.")
        return {}

def sending(state: State) -> dict:
    """Sends the final approved/edited draft. No LLM step, no tool binding.

    `registry.py`'s `TOOL_PERMISSIONS["sending"]` is `[]` -- `send_email`
    is never offered to a model, so there's no decision to make here,
    unlike `unauthenticated`/`reading`. Delegates directly to `send_email`
    (`app/tools/email_ops.py`) rather than inlining its logic -- same
    shape as `guard()` calling `get_injection_type()`: this node just uses
    the result of a separate, non-tool function.
    """
    return send_email(state)