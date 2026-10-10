"""
Live-trace validation gate.

Reads the captured investigations and checks the claim the whole demo rests on:
that every figure the copilot states came back from an approved tool, and that
it never reached around them.

For each question it reports the tool calls and arguments, the tools whose
output was actually used, the final answer, and four verdicts:

  NUMBER SOURCING   every important figure in the answer traces to a tool result
  NO SQL            the model never emitted SQL, in prose or in tool arguments
  NO RAW ACCESS     no call outside the approved registry, no file paths
  TOOL COVERAGE     the question triggered the tools it should have

"Important figure" means a number carrying a currency symbol, a percent sign, a
unit, or two or more digits - the numbers a director would repeat in a
meeting. Ordinals, dates and small counts are excluded; they are not what a
hallucination gate is for.

Run:  python validate_traces.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from api import agent, figure_check
from api.tools import REGISTRY

ROOT = Path(__file__).parent

# Which tools a question is expected to reach for. Presence is checked, not order.
EXPECTED = {
    "reliability this month": {"any": ["get_reported_reliability", "get_delivered_reliability",
                                       "get_gap_anatomy"]},
    "reported reliability different": {"any": ["get_gap_anatomy", "get_reported_reliability",
                                              "get_delivered_reliability"]},
    "missing from the alert queue": {"any": ["get_alert_blindspot", "find_silent_failures"]},
    # "which charger" is a ranking question: the portfolio plus evidence answers
    # it. Costing is good practice but not required to name the unit, and any
    # figure quoted still has to be sourced by the number-sourcing check.
    "first engineer": {"any": ["find_silent_failures"],
                       "also": ["get_charger_evidence"]},
    "why chg-0034": {"any": ["get_charger_evidence"],
                     "also": ["estimate_commercial_impact"]},
    "commercial impact": {"any": ["estimate_commercial_impact", "get_alert_blindspot",
                                  "find_silent_failures"]},
    "operations email": {"any": ["draft_ops_action"]},
    "commercial director": {"any": ["get_gap_anatomy", "get_alert_blindspot",
                                    "find_silent_failures"]},
}

SQL_RE = re.compile(r"\b(select\s+.+\s+from|insert\s+into|group\s+by|left\s+join|"
                    r"inner\s+join|where\s+\w+\s*=)\b", re.I)
RAW_RE = re.compile(r"(raw_\w+\.csv|_manifest|\.parquet|\.duckdb|voltway\.duckdb|/data/)", re.I)
MONTHS = (r"january|february|march|april|may|june|july|august|september|october|"
          r"november|december")


# The check itself lives with the application, so the gate asserts on exactly
# what the server computes rather than on a second copy that can drift.
numbers_in = lambda obj, out: figure_check.pool_from(obj, out)          # noqa: E731
matches = figure_check.matches


def important_numbers(text: str) -> list[tuple[str, float]]:
    return [(f["text"], f["value"]) for f in figure_check.figures_in(text)]


def expectation_for(question: str):
    q = question.lower()
    for key, spec in EXPECTED.items():
        if key in q:
            return spec
    return None


def main() -> int:
    traces = agent.list_traces()
    if not traces:
        print("\nNo traces found. Run: python capture_traces.py\n")
        return 1

    failures = 0
    print("\n" + "=" * 78)
    print("  LIVE-TRACE VALIDATION GATE")
    print("=" * 78)

    for meta in traces:
        t = json.loads((agent.TRACE_DIR / meta["file"]).read_text(encoding="utf-8"))
        q, answer = t["question"], t["answer"]
        calls = t["tool_calls"]

        print("\n" + "-" * 78)
        print("Q  {}".format(q))
        print("   trace: traces/{}   ·   captured {}".format(meta["file"], t.get("captured_at")))
        print()
        print("   TOOL CALLS")
        for c in calls:
            args = json.dumps(c["arguments"]) if c["arguments"] else "{}"
            print("     {:<28} {:<44} {:>5}ms".format(c["tool"], args[:44], c["ms"]))
            if c.get("source_layer"):
                print("       source: {}  ·  as_of {}".format(
                    c["source_layer"][:56], c.get("as_of")))

        # ---- pool of every number any called tool actually returned.
        # The recording holds the full results, so this is reproducible without
        # a running semantic layer.
        pool: set[float] = set()
        recorded = 0
        for c in calls:
            numbers_in(c.get("result_summary") or {}, pool)
            if c.get("result"):
                numbers_in(c["result"], pool)
                recorded += 1

        # Re-running the tools is a supplement, not the source of truth. If it
        # fails, that is a fact about the environment, not about the answer.
        live_failures = []
        for c in calls:
            try:
                pool = numbers_in(REGISTRY[c["tool"]](**c["arguments"]), pool)
            except Exception as exc:                            # noqa: BLE001
                live_failures.append("{}: {}".format(c["tool"], str(exc)[:60]))

        verifiable = recorded == len(calls) or not live_failures

        nums = important_numbers(answer)
        unsourced = [(txt, v) for txt, v in nums if not matches(v, pool)]

        ok_numbers = not unsourced and verifiable
        ok_sql = not SQL_RE.search(answer) and not any(
            SQL_RE.search(json.dumps(c["arguments"])) for c in calls)
        ok_raw = (all(c["tool"] in REGISTRY for c in calls)
                  and not RAW_RE.search(answer)
                  and not any(RAW_RE.search(json.dumps(c["arguments"])) for c in calls))

        spec = expectation_for(q)
        used = {c["tool"] for c in calls}
        if spec:
            ok_tools = bool(used & set(spec["any"])) and all(
                x in used for x in spec.get("also", []))
        else:
            ok_tools = bool(used)

        print()
        print("   ANSWER")
        for line in answer.split("\n"):
            if line.strip():
                print("     " + line.strip()[:112])
        print()
        print("   CHECKS")
        if not verifiable:
            print("     SKIP  number sourcing      could not verify: no recorded "
                  "results and the tools did not answer")
            for f in live_failures[:3]:
                print("            {}".format(f))
        else:
            print("     {}  number sourcing      {} important figures, {} unsourced".format(
                "PASS" if ok_numbers else "FAIL", len(nums), len(unsourced)))
            if unsourced:
                for txt, v in unsourced[:6]:
                    print("            unsourced: {!r} ({})".format(txt, v))
            if live_failures:
                print("            (pool came from the recording; {} tool re-run(s) "
                      "unavailable)".format(len(live_failures)))
        print("     {}  no SQL emitted".format("PASS" if ok_sql else "FAIL"))
        print("     {}  no raw or ground-truth access".format("PASS" if ok_raw else "FAIL"))
        print("     {}  expected tools used   {}".format(
            "PASS" if ok_tools else "FAIL", " · ".join(sorted(used)) or "(none)"))

        failures += sum(0 if x else 1 for x in (ok_numbers, ok_sql, ok_raw, ok_tools))

    print("\n" + "=" * 78)
    print("  {} traces · {}".format(
        len(traces), "all checks passed" if not failures
        else "{} CHECKS FAILED".format(failures)))
    print("=" * 78 + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
