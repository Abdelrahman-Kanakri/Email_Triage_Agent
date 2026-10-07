"""Interactive terminal client for the triage graph.

    uv run python -m app.main

Loop: send a message -> stream node updates -> if the graph paused at an
`interrupt()`, ask the human and resume with `Command(resume=...)` ->
repeat until the run reaches END -> prompt for the next message. Typing
`exit` ends the session; the graph itself never "waits", only the CLI does.

Uses the in-memory checkpointer: one process = one session. The API
(`app.api`) is the persistent, multi-session surface.
"""

import uuid

from langchain_core.messages import AIMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from app.core import get_logger
from app.graph.build import build_graph, initial_state, run_config

logger = get_logger(__name__)


def _print_updates(graph: CompiledStateGraph, payload: object, config: dict) -> None:
    """Stream one run segment, printing which node ran and anything worth showing."""
    for chunk in graph.stream(payload, config, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__":
                continue
            print(f"  · {node}")
            if not isinstance(update, dict):
                continue
            for msg in update.get("messages", []):
                if isinstance(msg, AIMessage) and msg.content:
                    print(f"\nAgent: {msg.content}\n")
            for flagged in update.get("flagged_emails", []):
                print(
                    f"    ⚑ flagged [{flagged.category}] {flagged.email.subject}: {flagged.reason}"
                )


def _ask(prompt: str, options: list[str]) -> str:
    while True:
        answer = input(f"{prompt} [{'/'.join(options)}]: ").strip().lower()
        if answer in options:
            return answer
        print(f"  Please type one of: {', '.join(options)}")


def _resume_value(value: dict) -> dict:
    """Turn one interrupt payload into the resume dict its node expects."""
    kind = value.get("kind")
    if kind == "auth":
        return {"decision": _ask(value["question"], ["approve", "deny"])}
    if kind == "approval":
        email, draft = value["email"], value["draft"]
        print("\n" + "=" * 70)
        print(f"FROM: {email['sender']}\nSUBJECT: {email['subject']}\n\n{email['body']}")
        print("-" * 70)
        print(f"DRAFT -> {draft['recipient']}\nSUBJECT: {draft['subject']}\n\n{draft['body']}")
        print("=" * 70)
        return {"decision": _ask("Decision", ["approve", "edit", "reject"])}
    if kind == "edit":
        draft = value["draft"]
        subject = input(f"New subject (enter = keep '{draft['subject']}'): ").strip()
        print("New body (finish with a single '.' line; empty = keep):")
        lines: list[str] = []
        while (line := input()) != ".":
            lines.append(line)
        return {
            "subject": subject or draft["subject"],
            "body": "\n".join(lines) or draft["body"],
        }
    if kind == "reject_reason":
        return {"reason": input("Reason for rejection: ").strip()}
    # Unknown payload: show it raw and pass the answer through.
    return {"decision": input(f"{value}\n> ").strip()}


def run_turn(graph: CompiledStateGraph, payload: object, config: dict) -> None:
    """Run until END, answering every interrupt along the way."""
    _print_updates(graph, payload, config)
    while (snapshot := graph.get_state(config)).interrupts:
        resume = _resume_value(snapshot.interrupts[0].value)
        _print_updates(graph, Command(resume=resume), config)

    values = graph.get_state(config).values
    flagged = values.get("flagged_emails", [])
    print(
        f"\nDone. Processed {len(values.get('processed_ids', []))} email(s), {len(flagged)} flagged.\n"
    )


def main() -> None:
    graph = build_graph()
    thread_id = str(uuid.uuid4())
    config = run_config(thread_id, surface="cli")
    logger.info("cli session started", thread_id=thread_id)
    print("Email Triage Agent — type a request (e.g. 'triage my inbox'), or 'exit'.\n")

    first = True
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in {"exit", "quit"}:
            break
        if not text:
            continue
        payload = initial_state(text) if first else {"messages": initial_state(text)["messages"]}
        first = False
        run_turn(graph, payload, config)

    logger.info("cli session ended", thread_id=thread_id)
    print("Bye.")


if __name__ == "__main__":
    main()
