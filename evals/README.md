# Evaluation suite

The suite contains 25 questions from direct lookups through rankings, proxy
reasoning, source-boundary checks, ambiguous assignment prompts, and a
conversational follow-up. Each case defines:

- the expected tool calls;
- direct tool calls that produce fresh reference evidence;
- a written reference answer describing the correct conclusion and caveats.

The runner asks the airport agent, checks its actual tool-call set, and then asks
an LLM judge to compare the answer with the fresh evidence. This avoids stale
golden numbers for live FAA, weather, and OpenSky data. Historical answers are
also checked against the source adapters rather than copied into the judge prompt.

The judge scores tool selection (2), evidence fidelity (4), reasoning (2), and
communication (2). A case passes at 8/10 or higher only when the expected tool
contract passes, evidence fidelity is at least 3/4, and there are no critical
errors. Critical errors include unsupported numbers, wrong periods or populations,
invented causes, hidden assumptions, internal workflow narration, and presenting
a proxy as measured demand, capacity, or profitability.

List the cases:

```sh
uv run --env-file .env python evals/run.py --list
```

Run a small targeted set while developing:

```sh
uv run --env-file .env python evals/run.py \
  --case three-airport-growth-screen \
  --case sfo-demand-pressure \
  --case assignment-anc-clarification
```

Run the full suite:

```sh
uv run --env-file .env python evals/run.py --all
```

The agent and judge default to `openai/gpt-5.6-terra`. Set
`EVAL_JUDGE_MODEL` to use a different OpenRouter judge. Full runs make live API
requests and at least two model calls per case, so targeted runs are faster while
iterating. JSON reports are written to `evals/results/` and excluded from Git.

