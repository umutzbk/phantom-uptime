"""
Runs the demo questions live and records each investigation to traces/.

The recordings are what replay mode serves. They are not mocks: each one is a
real run of the same agent loop against the same Cube instance, captured so a demo
does not depend on network access or an API key.

Prerequisites:  docker compose up -d  ·  ANTHROPIC_API_KEY in .env
Run:            python capture_traces.py
"""
from __future__ import annotations

import sys
import time

from dotenv import load_dotenv

load_dotenv()

from api import agent                                   # noqa: E402
from api.main import DEMO_QUESTIONS                     # noqa: E402


def main() -> int:
    print("\nCapturing live investigations  ·  model {}  ·  effort {}\n".format(
        agent.MODEL, agent.EFFORT))
    failed = 0
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        print("{}/{}  {}".format(i, len(DEMO_QUESTIONS), q))
        t0 = time.time()
        try:
            trace = agent.run(q)
        except Exception as exc:                        # noqa: BLE001
            print("      FAILED: {}\n".format(str(exc)[:200]))
            failed += 1
            continue
        if not trace["answer"]:
            print("      FAILED: no answer produced\n")
            failed += 1
            continue
        path = agent.save_trace(trace)
        tools = " -> ".join(c["tool"] for c in trace["tool_calls"]) or "(none)"
        print("      {:.1f}s  {} tool calls".format(time.time() - t0, len(trace["tool_calls"])))
        print("      {}".format(tools))
        print("      saved {}\n".format(path.name))

    print("{} captured, {} failed\n".format(len(DEMO_QUESTIONS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
