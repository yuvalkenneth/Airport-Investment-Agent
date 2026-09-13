"""Offline checks: real agent and tools, scripted model responses."""

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from httpx import ConnectError
from langchain.agents import create_agent
from langchain.messages import AIMessage, ToolMessage
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from pydantic import Field

from airport_agent.cli import chat, main
from airport_agent.constants import AGENT_RECURSION_LIMIT, SYSTEM_PROMPT
from airport_agent.tools import TOOLS, calculate_percentage


ASSESSMENT_QUESTION_CASES = (
    {
        "question": "Which New England airports are strong terminal expansion candidates?",
        "must_clarify": ("analysis period", "ranking objective"),
        "suggested_default": "explicit months compared with the same months one year earlier",
    },
    {
        "question": "Compare LA and Santa Ana congestion.",
        "must_clarify": ("LAX and SNA", "analysis period", "congestion measure"),
        "suggested_default": "arrival delay and cancellation rates for the latest complete year",
    },
    {
        "question": "What is the percentage of long-haul flights out of Anchorage airport?",
        "must_clarify": ("analysis period",),
        "suggested_default": "observed departures in an explicit completed UTC day; routes over 3,000 statute miles",
    },
    {
        "question": "What is the unmet flight demand at SFO and why?",
        "must_clarify": ("analysis period", "proxy for unmet demand"),
        "suggested_default": "passenger growth versus seat growth in explicit months against last year, with occupancy and domestic delays as supporting evidence",
    },
)


class ScriptedModel(FakeMessagesListChatModel):
    seen: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, **kwargs)


def tool_call(call_id, part, total):
    return AIMessage(content="", tool_calls=[{
        "name": "calculate_percentage",
        "args": {"part": part, "total": total},
        "id": call_id,
        "type": "tool_call",
    }])


class AgentChecks(unittest.TestCase):
    def test_assessment_question_contracts(self):
        self.assertEqual(len(ASSESSMENT_QUESTION_CASES), 4)
        for case in ASSESSMENT_QUESTION_CASES:
            self.assertTrue(case["question"])
            self.assertTrue(case["must_clarify"])
            self.assertTrue(case["suggested_default"])
        self.assertIn("ask one concise clarification", SYSTEM_PROMPT.lower())
        self.assertIn("estimate using stated proxies", SYSTEM_PROMPT)

    def test_tool_loop_followup_and_clear(self):
        model = ScriptedModel(responses=[
            tool_call("first", 25, 100),
            tool_call("second", 8, 40),
            AIMessage(content="25% and 20%."),
            AIMessage(content="The first share was larger."),
            AIMessage(content="Starting fresh."),
        ])
        agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
        output = io.StringIO()
        with patch("builtins.input", side_effect=[
            "  ", "Compare these hypothetical shares.", "Which was larger?",
            "/clear", "Start again.", "/exit",
        ]), redirect_stdout(output):
            chat(agent, AGENT_RECURSION_LIMIT)

        self.assertEqual(len(model.seen), 5)
        tool_results = [m for m in model.seen[2] if isinstance(m, ToolMessage)]
        self.assertEqual([json.loads(m.content)["percentage"] for m in tool_results], [25, 20])
        self.assertIn("Compare these hypothetical shares.", [m.content for m in model.seen[3]])
        self.assertEqual([m.type for m in model.seen[4]], ["system", "human"])
        self.assertEqual(output.getvalue().count("Tool: calculate_percentage"), 2)
        self.assertIn('"part": 25', output.getvalue())
        self.assertIn('"percentage": 25', output.getvalue())
        self.assertEqual(output.getvalue().count("Agent > 25% and 20%."), 1)
        self.assertNotIn("Connected tools:", output.getvalue())
        self.assertIn("Agent > 25% and 20%.", output.getvalue())

    def test_bad_tool_input_returns_error_and_can_recover(self):
        model = ScriptedModel(responses=[
            tool_call("bad", 2, 0), tool_call("fixed", 2, 4), AIMessage(content="50%."),
        ])
        agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
        result = agent.invoke({"messages": [("user", "Calculate the share.")]})
        self.assertEqual(model.seen[1][-1].status, "error")
        self.assertEqual(json.loads(model.seen[2][-1].content)["percentage"], 50)
        self.assertEqual(result["messages"][-1].text, "50%.")
        for part, total in [(1, 0), (-1, 2), (3, 2), (float("nan"), 2), (1, float("inf"))]:
            self.assertIsInstance(calculate_percentage.invoke({"part": part, "total": total}), str)

    def test_step_limit_and_connection_failure_keep_previous_history(self):
        model = ScriptedModel(responses=[AIMessage(content="Remembered.")])
        agent = create_agent(model=model, tools=TOOLS, system_prompt=SYSTEM_PROMPT)
        stream_events = agent.stream_events
        calls = 0

        def stream_with_failure(state, config, version):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ConnectError("offline test")
            if calls == 3:
                endless = ScriptedModel(responses=[
                    tool_call(f"repeat-{i}", 1, 2) for i in range(5)
                ])
                looping_agent = create_agent(model=endless, tools=TOOLS)
                return looping_agent.stream_events(state, config={"recursion_limit": 4}, version=version)
            return stream_events(state, config=config, version=version)

        output = io.StringIO()
        with patch.object(agent, "stream_events", side_effect=stream_with_failure), patch(
            "builtins.input", side_effect=["Remember this.", "Offline turn.", "Loop forever.", "Follow up.", EOFError],
        ), redirect_stdout(output):
            chat(agent, AGENT_RECURSION_LIMIT)
        self.assertIn("OpenRouter request failed", output.getvalue())
        self.assertIn("step limit", output.getvalue())
        self.assertEqual(
            [m.content for m in model.seen[-1] if m.type == "human"],
            ["Remember this.", "Follow up."],
        )

    def test_missing_key_has_setup_message(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": ""}), redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertIn("OPENROUTER_API_KEY", output.getvalue())


if __name__ == "__main__":
    unittest.main()
