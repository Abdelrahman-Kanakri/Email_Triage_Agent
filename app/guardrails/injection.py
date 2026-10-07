"""Prompt-injection / jailbreak guard, run inside the `reading` node.

Two-tier check, cheap-deterministic-first: a regex scan for known
injection/jailbreak patterns runs first and is trusted immediately on a
match -- fast, free, and a deterministic pattern can't be talked out of
a verdict the way an LLM theoretically could. A non-match is NOT treated
as "clean" -- it only means this filter didn't catch anything, not that
nothing is wrong -- so it escalates to `model_with_structured_output`,
a tool-less classifier LLM call that is the real authority on
"clean" vs. "ambiguous" vs. "injection".

Never exposed as a tool (`.bind_tools()`) -- least privilege applied
recursively, so even a successful injection here has nothing to call.
"""

import re

from langchain_openai import ChatOpenAI
from structlog.types import FilteringBoundLogger

from app.core import settings
from app.graph.prompts import INJECTION_DETECTION_PROMPT
from app.graph.state import ClassifierResult, Email


# a function to create a Logger for this module
def create_logger() -> FilteringBoundLogger:
    from app.core import get_logger

    return get_logger(__name__)


# Define the LLM for injection detection with the SchemaModel output parser
model = ChatOpenAI(
    model=settings.OPENAI_MINI_MODEL,
    temperature=0.0,
    api_key=settings.OPENAI_API_KEY.get_secret_value(),
)
# wrap the model with structured output to ensure it returns a ClassifierResult
model_with_structured_output = model.with_structured_output(ClassifierResult)


def get_injection_type(email: Email) -> str:
    """
    A function that returns the injection type based on the input email.

    returns:
        - "clean" if the input email is "clean"
        - "injection" if the input email is "injection"
        - "ambiguous" if the input email is "ambiguous"
    """
    logger = create_logger()
    # Check if the inbox is empty and raise an error if it is
    if not email.body:
        logger.warning("Empty body, can't classify", message_id=email.message_id)
        raise ValueError("Email is empty. Cannot determine injection type.")

    # A regex pattern to check if the email body contains any injection keywords
    injection_keywords = (
        r"(?i)("
        r"DROP\s+TABLE|UNION\s+SELECT|"
        r"<script\b|javascript:|onerror\s*=|"
        r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|prompts)|"
        r"disregard\s+(all\s+)?system\s+instructions|"
        r"you\s+are\s+now\s+in\s+DAN\s+mode|"
        r"act\s+as\s+an\s+unrestricted|"
        r"\[SYSTEM\]|\[/INST\]|<\|im_start\|>"
        r")"
    )

    email_body = email.body
    # Two-tier guard, not a single check. A regex match is trusted immediately
    # (cheap, instant, and a deterministic pattern can't be talked out of a
    # verdict the way an LLM theoretically could). A non-match is NOT treated
    # as "clean" -- absence of a known bad phrase only proves this filter
    # didn't catch anything, not that nothing is wrong. So the only thing the
    # heuristic is ever allowed to decide on its own is "injection"; every
    # other case escalates to the LLM, which is the actual authority here.
    if re.search(injection_keywords, email_body):
        logger.info(
            "regex caught it",
            message_id=email.message_id,
            tier="regex",
            verdict="injection",
        )
        return "injection"
    else:
        # with_structured_output forces the reply into a validated
        # ClassifierResult (see state.py for why that's a single 3-way field
        # now, not a bool) -- so injection_type is already guaranteed to be
        # exactly "clean"/"injection"/"ambiguous". No re-branching needed;
        # whatever the LLM decided is passed through as the final answer.
        # A plain model.invoke(...) (no with_structured_output wrapper) would
        # instead return an AIMessage, requiring .content to reach raw text --
        # here injection_result is the ClassifierResult itself, read directly.
        injection_result = model_with_structured_output.invoke(
            INJECTION_DETECTION_PROMPT.format(email_body=email_body)
        )
        logger.info(
            "LLM Decide",
            message_id=email.message_id,
            tier="LLM",
            verdict=injection_result.injection_type,
        )
        return injection_result.injection_type
