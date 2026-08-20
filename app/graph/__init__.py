"""Graph layer: state schema for the email triage agent.

Only `State` is re-exported here. `state.py` has zero `app.*` imports of
its own, so touching it is safe no matter what order the project's
modules get imported in.

`nodes.py` is deliberately NOT re-exported from this package's
`__init__.py`. It depends on `app.core`, which depends on
`app.guardrails`, and `app.guardrails.injection` imports
`app.graph.prompts` at module level -- a real cycle back into this
package. If this `__init__.py` eagerly imported `nodes.py` too, then the
*first* time anything anywhere touched even `app.graph.prompts` (a plain
leaf submodule, no dependencies of its own), Python would have to run
this file to completion first -- which pulls in `app.core`, which can
already be mid-import at that exact moment
(`app.core.logging -> app.guardrails -> app.guardrails.injection ->
app.graph.prompts` is a real call path that exists today). Verified by
testing four fresh-interpreter entry points (`import app.graph`,
`app.tools`, `app.guardrails`, `app.core` first) -- an earlier version of
this file that also imported `nodes.py` failed 3 of the 4 with
`ImportError: cannot import name '...' from partially initialized
module`.

Node functions are imported straight from the submodule instead --
`from app.graph.nodes import unauthenticated, ...` -- the same pattern
already used elsewhere in this project (`get_injection_type` is imported
directly from `app.guardrails.injection`, not through that package's own
re-export, even though it is re-exported there too).
"""

from app.graph.state import State

__all__ = ["State"]
