"""Terminal entry point for the airport investment agent."""

from httpx import HTTPError
from langchain.messages import HumanMessage
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphRecursionError
from openrouter.errors import OpenRouterError
from pydantic import ValidationError

from airport_agent.agent import build_agent
from airport_agent.config import Settings


class ToolProgress(BaseCallbackHandler):
    def on_tool_start(self, serialized, input_str, **kwargs):
        print(f"  Tool: {serialized['name']}", flush=True)


def chat(agent, recursion_limit: int) -> None:
    # ponytail: session-only history; add persistence and trimming for longer use.
    history = []
    config = {"recursion_limit": recursion_limit, "callbacks": [ToolProgress()]}
    print("Ask a question. /clear starts over; /exit quits.")
    print("Connected tools: percentage calculation. Airport APIs come next.\n")

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
            result = agent.invoke(
                {"messages": [*history, HumanMessage(content=prompt)]}, config=config
            )
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
        print(f"Agent > {history[-1].text}\n")


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
