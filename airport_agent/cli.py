"""Terminal entry point for the airport investment agent."""

import json
import os
import sys

from httpx import HTTPError
from langchain.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from openrouter.errors import OpenRouterError
from pydantic import ValidationError

from airport_agent.agent import build_agent
from airport_agent.config import Settings



def pretty(value) -> str:
    """Render tool messages and JSON without Python content-block wrappers."""
    if hasattr(value, "content"):
        value = value.content
    if isinstance(value, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in value
    ):
        value = "".join(block["text"] for block in value)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def print_stream(stream):
    """Consume live text and tool execution events, then return final state."""
    def label(text):
        if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
            text = f"\033[1;36m{text}\033[0m"
        print(text, end="", flush=True)

    for kind, item in stream.interleave("messages", "tool_calls"):
        if kind == "messages":
            started = False
            for token in item.text:
                if not token:
                    continue
                if not started:
                    label("Agent > ")
                    started = True
                print(token, end="", flush=True)
            if started:
                print("\n", flush=True)
        elif kind == "tool_calls":
            label(f"Tool: {item.tool_name}\n")
            print(pretty(item.input), flush=True)
            started = False
            for delta in item.output_deltas:
                if not started:
                    label("Progress > ")
                    started = True
                print(pretty(delta), end="", flush=True)
            if started:
                print(flush=True)
            output = item.output
            error = item.error
            failed = error is not None or getattr(output, "status", None) == "error"
            label("Tool error > " if failed else "Result > ")
            print(pretty(error if error is not None else output), end="\n\n", flush=True)
    return stream.output


def chat(agent, recursion_limit: int) -> None:
    # ponytail: session-only history; add persistence and trimming for longer use.
    history = []
    config = {"recursion_limit": recursion_limit}
    print("Ask a question. /clear starts over; /exit quits.")

    while True:
        try:
            prompt = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if not prompt:
            continue
        if prompt.lower() in {"/exit", "/quit", "exit", "quit"}:
            print("Goodbye.")
            return
        if prompt.lower() == "/clear":
            history = []
            print("Conversation cleared.\n")
            continue

        print("Agent is working...", flush=True)
        try:

            stream = agent.stream_events(
                {"messages": [*history, HumanMessage(content=prompt)]},
                config=config, version="v3",
            )
            result = print_stream(stream)
        except KeyboardInterrupt:
            print("\nCancelled. Previous conversation kept.\n")
            continue
        except GraphRecursionError:
            print("Agent reached its step limit. Try a narrower question.\n")
            continue
        except (OpenRouterError, HTTPError) as exc:
            status = getattr(exc, "status_code", "connection error")
            print(
                f"OpenRouter request failed ({status}). Check your key, credits, "
                "model, or connection and try again.\n"
            )
            continue

        history = result["messages"]


def main() -> int:
    try:
        settings = Settings.from_environment()
    except ValidationError:
        print(
            "Set OPENROUTER_API_KEY in .env, then run:\n"
            "  uv run --env-file .env python -m airport_agent.cli"
        )
        return 1

    print(f"Airport Investment Agent | OpenRouter | {settings.model}\n")
    chat(build_agent(settings), settings.agent_recursion_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
