"""
...
"""
from langchain_mistralai import ChatMistralAI

from app.core import get_logger, settings
from app.graph.prompts import SYSTEM_PROMPT_REASON
from app.graph.state import FlaggedEmail, SmallFlaggedEmail, State
from app.guardrails.injection import get_injection_type
from app.tools import TOOL_PERMISSIONS

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
    return {"messages": [result]}


def reading(state: State) -> dict: 
    """
    Calls the model with `fetch_inbox` bound; the model decides whether
    to call it. Returns the response message only -- tool execution of the fetch_inbox tool 
    and updated inbox state.
    """
    result = model_with_fetch_inbox_tool.invoke(state["messages"])
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
            + state["messages"][-1].content
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
            + state["messages"][-1].content
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