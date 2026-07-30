"""State-to-tool permission matrix for the email triage agent.

Keys are the six FSM states from the Phase 1 design (unauthenticated,
reading, drafting, awaiting_approval, sending, done) — not field names
from app.graph.state.State. Every state is listed explicitly, even where
no tool is authorized, so TOOL_PERMISSIONS[state_name] is always a safe
lookup and never needs a default or a KeyError guard.
"""

from app.tools.email_ops import auth_tool_access, fetch_inbox

TOOL_PERMISSIONS = {
    "unauthenticated": [auth_tool_access],
    "reading": [fetch_inbox],
    "drafting": [],
    "awaiting_approval": [],
    "sending": [], 
    "done": [],  
}