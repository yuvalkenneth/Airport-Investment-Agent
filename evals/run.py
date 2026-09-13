"""Run tool-grounded conversational evaluations against the airport agent."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

from langchain.messages import HumanMessage, ToolMessage
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from airport_agent.agent import build_agent
from airport_agent.config import Settings
from airport_agent.tools import TOOLS


CASES_PATH = Path(__file__).with_name("cases.json")
RESULTS_DIR = Path(__file__).with_name("results")

JUDGE_PROMPT = """You are grading an airport-investment agent. Compare its final
answer and actual tool activity with the fresh reference evidence and written
reference answer. Tool names are evaluator evidence and should not appear in the
agent's user-facing response.

Score four dimensions:
- tool_selection (0-2): appropriate calls, arguments, periods, and no redundant calls;
- evidence_fidelity (0-4): numbers, rankings, definitions, populations, and sources;
- reasoning (0-2): conclusion follows the KPI and does not overstate proxies or causality;
- communication (0-2): direct, clear, decision-relevant, with material assumptions and caveats.

For clarification cases, a correct answer proposes a defensible default for any
missing period and asks one concise clarification only for material ambiguity that
cannot be resolved safely, before collecting data. For source-boundary cases,
reward a precise limitation rather
than a fabricated answer. Optional tool names may be used but are not required;
no call is correct when the answer only states a known source limitation and gives
no data-derived number. Fresh reference evidence is unknown to the agent, so any
number in its answer must also be grounded in its actual tool results. Treat
unsupported numbers, wrong periods/populations,
invented causes, hidden assumptions, internal workflow narration, and proxy-to-
fact claims as critical errors. Supported but unrequested detail is a reasoning or
communication deduction, not a critical error unless it changes the conclusion or
violates a source boundary. Small rounding differences are acceptable.
Return only the requested structured verdict."""


class JudgeVerdict(BaseModel):
    tool_selection: int = Field(ge=0, le=2)
    evidence_fidelity: int = Field(ge=0, le=4)
    reasoning: int = Field(ge=0, le=2)
    communication: int = Field(ge=0, le=2)
    critical_errors: list[str] = Field(default_factory=list)
    explanation: str


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _compact(value: Any) -> Any:
    """Keep judge context small without removing decision-relevant totals."""
    value = _json_value(value)
    if isinstance(value, dict):
        return {
            key: _compact(item)
            for key, item in value.items()
            if key not in {"cache", "flights"}
        }
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _tool_calls(messages: list) -> list[dict]:
    outputs = {
        message.tool_call_id: _compact(message.content)
        for message in messages if isinstance(message, ToolMessage)
    }
    calls = []
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            calls.append({
                "name": call["name"],
                "args": call.get("args", {}),
                "result": outputs.get(call.get("id")),
            })
    return calls


def _run_turn(agent, history: list, question: str, recursion_limit: int) -> tuple[list, str, list[dict]]:
    before = len(history)
    result = agent.invoke(
        {"messages": [*history, HumanMessage(content=question)]},
        config={"recursion_limit": recursion_limit},
    )
    messages = result["messages"]
    turn_messages = messages[before:]
    return messages, _text(messages[-1].content), _tool_calls(turn_messages)


def _reference_evidence(case: dict, tools: dict) -> tuple[list[dict], list[str]]:
    evidence, errors = [], []
    for call in case.get("reference_calls", []):
        result = tools[call["tool"]].invoke(call["args"])
        parsed = _json_value(result)
        evidence.append({"tool": call["tool"], "args": call["args"], "result": _compact(parsed)})
        if isinstance(parsed, str):
            errors.append(f'{call["tool"]}: {parsed}')

    if errors:
        return evidence, errors
    if case.get("reference_derivation") == "long_haul_share":
        all_flights, long_haul = (item["result"] for item in evidence[:2])
        args = {"part": long_haul["matching_flights"], "total": all_flights["observed_flights"]}
        evidence.append({"tool": "calculate_percentage", "args": args,
                         "result": tools["calculate_percentage"].invoke(args)})
    elif case.get("reference_derivation") == "route_distance":
        origin, destination = (item["result"] for item in evidence[:2])
        args = {
            "origin_lat": origin["lat"], "origin_lon": origin["lon"],
            "destination_lat": destination["lat"], "destination_lon": destination["lon"],
        }
        evidence.append({"tool": "calculate_route_distance", "args": args,
                         "result": tools["calculate_route_distance"].invoke(args)})
    return evidence, errors


def _route_check(expected: list[str], actual: list[dict], optional: list[str] | None = None) -> dict:
    actual_names = [call["name"] for call in actual]
    optional = optional or []
    missing = list((Counter(expected) - Counter(actual_names)).elements())
    unexpected = list((Counter(actual_names) - Counter(expected + optional)).elements())
    return {"passed": not missing and not unexpected, "missing": missing,
            "unexpected": unexpected, "actual": actual_names, "optional": optional}


def _select_cases(cases: list[dict], ids: list[str], run_all: bool) -> list[dict]:
    if run_all:
        return cases
    by_id = {case["id"]: case for case in cases}
    unknown = sorted(set(ids) - set(by_id))
    if unknown:
        raise SystemExit(f"Unknown case id(s): {', '.join(unknown)}")
    return [by_id[case_id] for case_id in ids]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--all", action="store_true", help="Run all cases")
    choice.add_argument("--case", action="append", default=[], help="Run one case id; repeatable")
    choice.add_argument("--list", action="store_true", help="List cases and exit")
    args = parser.parse_args()

    cases = json.loads(CASES_PATH.read_text())
    if args.list:
        for case in cases:
            print(f'{case["id"]}: {case["question"]}')
        return 0
    selected = _select_cases(cases, args.case, args.all)
    settings = Settings.from_environment()
    judge_model = os.getenv("EVAL_JUDGE_MODEL", "").strip() or settings.model
    judge = ChatOpenRouter(
        model=judge_model, api_key=settings.api_key,
        timeout=settings.model_timeout_ms, max_retries=settings.model_max_retries,
    ).with_structured_output(JudgeVerdict)
    tools = {tool.name: tool for tool in TOOLS}
    results = []

    for index, case in enumerate(selected, 1):
        print(f'[{index}/{len(selected)}] {case["id"]}', flush=True)
        reference, reference_errors = _reference_evidence(case, tools)
        if reference_errors:
            results.append({"id": case["id"], "status": "skipped_reference_error",
                            "reference_errors": reference_errors})
            print("  skipped: reference data unavailable", flush=True)
            continue

        agent = build_agent(settings)
        history, setup_calls = [], []
        if case.get("setup_question"):
            history, _, setup_calls = _run_turn(
                agent, history, case["setup_question"], settings.agent_recursion_limit,
            )
        _, answer, actual_calls = _run_turn(
            agent, history, case["question"], settings.agent_recursion_limit,
        )
        route = _route_check(case["expected_tools"], actual_calls, case.get("optional_tools"))
        setup_route = _route_check(case.get("setup_expected_tools", []), setup_calls)
        payload = {
            "evaluation_date_utc": datetime.now(UTC).date().isoformat(),
            "question": case["question"],
            "category": case["category"],
            "written_reference_answer": case["reference_answer"],
            "fresh_reference_evidence": reference,
            "expected_tool_names": case["expected_tools"],
            "optional_tool_names": case.get("optional_tools", []),
            "actual_tool_calls": actual_calls,
            "deterministic_route_check": route,
            "setup_route_check": setup_route if case.get("setup_question") else None,
            "agent_answer": answer,
        }
        verdict = judge.invoke([
            ("system", JUDGE_PROMPT),
            ("human", json.dumps(payload, ensure_ascii=False, default=str)),
        ])
        scores = verdict.model_dump()
        total = sum(scores[key] for key in (
            "tool_selection", "evidence_fidelity", "reasoning", "communication"
        ))
        passed = (route["passed"] and setup_route["passed"]
                  and total >= case.get("minimum_score", 8)
                  and scores["evidence_fidelity"] >= 3 and not scores["critical_errors"])
        results.append({
            "id": case["id"], "status": "passed" if passed else "failed",
            "question": case["question"], "answer": answer,
            "expected_tools": case["expected_tools"], "actual_tool_calls": actual_calls,
            "setup_tool_calls": setup_calls, "route_check": route,
            "setup_route_check": setup_route if case.get("setup_question") else None,
            "reference_answer": case["reference_answer"], "reference_evidence": reference,
            "judge": {**scores, "total": total},
        })
        print(f'  {"PASS" if passed else "FAIL"} {total}/10 - {scores["explanation"]}', flush=True)

    passed_count = sum(item["status"] == "passed" for item in results)
    failed_count = sum(item["status"] == "failed" for item in results)
    skipped_count = len(results) - passed_count - failed_count
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "agent_model": settings.model, "judge_model": judge_model,
        "summary": {"passed": passed_count, "failed": failed_count,
                    "skipped": skipped_count, "total": len(results)},
        "results": results,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    output = RESULTS_DIR / f'{datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")}.json'
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    print(f"Report: {output}")
    print(f"Summary: {passed_count} passed, {failed_count} failed, {skipped_count} skipped")
    return 1 if failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
