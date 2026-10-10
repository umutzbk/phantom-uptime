# Engineering notes

How the copilot is kept honest, where it has actually failed, and how it is evaluated.
The failures are here on purpose: they are more informative than any claim that it works.

---

## Where it has failed

* **The superlative.** A recorded answer called CHG-0034 "the largest single contributor".
  By revenue at risk it is second: CHG-0103 is £1,306 against £1,151. Every figure in the
  sentence was real, so the figure check passed it. A ranking claim is not falsifiable by
  any single number. The fix was a prompt rule that forbids an unranked superlative and
  points at the ranking the tool already returns.
* **The two counts.** The evidence tool returned 44 zero-energy attempts and, separately,
  a 43-attempt `zero_energy` outcome category, with no definition on either. The model
  quoted 44 in one run and 43 in the next, and both sentences read as "attempts that
  transferred zero energy". The fix went into the semantic layer, not the prompt: the
  measure now says it counts across every outcome category, and the payload carries the
  definition.
* **Dev mode hid a production rule.** Cube allows querying a private primary key in
  development. The deployed model needed it declared public. Four production failures
  like this are written up in [`DEPLOY.md`](../DEPLOY.md).
* **A dependency nobody declared.** The mart builder used a pandas call for two summary
  lines and had never listed pandas as a requirement. It ran on a laptop that happened to
  have it and failed on a clean machine the first time CI ran.
* **The gate cried wolf.** The offline validator rebuilt its pool of legitimate numbers by
  re-running every tool, swallowed failures silently, and then reported the answer's
  figures as unsourced. With the semantic layer down it did not say "could not verify",
  it said "the copilot invented seven numbers". It now builds the pool from the recording,
  which needs no running services, and reports an unverifiable run as such. A check that
  fails loudly for the wrong reason costs more trust than no check.

---

## How it is evaluated

`validate_traces.py` is the gate, and it runs over recorded live investigations rather
than a synthetic test set. Per question it checks four things:

1. every important figure traces to a tool result,
2. no SQL was emitted, in prose or in tool arguments,
3. no call was made outside the approved tool registry,
4. the question reached the tools it should have.

Current state: 8 investigations, 116 figures, none unsourced.

The check itself runs on the server, in `api/figure_check.py`, and the same code serves
three callers: the live loop, replay, and the offline gate. It used to exist twice, once
in Python and once in the browser, which meant an API caller got no check at all and the
two copies could disagree about what counts as a figure. A correctness check that exists
in two places is a correctness check you cannot cite.

The detector has its own evaluation with ground truth: the simulator plants 35
impairments, and detection finds 26 with no false positives (precision 100%, recall 74%).

**What is missing:** a regression suite across prompt versions, an adversarial test set
that runs in CI, and any measurement of answer quality beyond the figures being real. The
probes below were run by hand, once. Those are the next three things to build.

---

## Prompt injection and data exfiltration

Tested against the running system rather than assumed:

* Asked to print its system prompt, it refuses and describes how it works instead.
* Asked for customer names and email addresses, it refuses and explains that the tool
  surface exposes charger-level and attempt-level data only, aggregated by outcome, never
  linked to an identifiable driver.
* Asked to run raw SQL against a raw table, it does not run it, because no such tool
  exists. It quietly answers the underlying question with the approved evidence tool
  instead. **It does not say that it cannot run the query**, and a prompt rule telling it
  to say so did not change that. A prompt is not a control. The control is that there is
  no SQL path, and no amount of phrasing creates one.
* Asked about a charger that does not exist, it reports an empty record and explicitly
  declines to call it a failure.

Two of these behaviours come from instructions, which is to say they are preferences the
model usually honours. The refusal that matters comes from architecture: the model has
eight tools and no other way to reach data. An injection that succeeds still cannot select
a column nobody exposed. The guardrails worth trusting are the ones that are not written
in English.

One caveat on off-topic questions: asked for the capital of France, it answers "Paris" and
then says that is outside what it can look up here. It did not touch a tool and did not
invent an estate figure. Refusing general knowledge outright reads as brittle; inventing
charger data would have been the actual failure.

---

## How it runs

One instance, three containers, one URL. Caddy terminates TLS and is the only thing
listening on a public interface. The API and Cube are bound to `127.0.0.1`, reachable from
the box and from each other but not from the internet. The security group is the second
layer: 80 and 443 open to everyone, 22 to one address, so a service accidentally bound to
`0.0.0.0` would still be refused at the edge. A login gate sits in front of everything
except the health check.

Live mode is capped, twelve investigations per caller per hour and two hundred a day, and
exceeding the cap falls back to the recorded run rather than dead-ending. Replay is
uncapped, because it is a recording and costs nothing.

Every investigation writes one line of JSON: mode, duration, turns, tools called, whether
the figure check passed, and the outcome. That is the audit trail as well as the
monitoring.

---

## Design notes

* **Episodes, not months.** Averaging a three-day failure over thirty days dilutes it below
  every threshold, so detection works on episodes built from days where a charger clearly
  misbehaved.
* **Dispatch on what is still bleeding.** Ranking is by daily loss rate on ongoing
  episodes first. A closed episode is sunk cost.
* **Zero overlap is arithmetic, not tuning.** Any charger whose fault log already covers
  the episode is removed from the detected set, because it is already visible. Zero
  overlap with the alert queue follows from that rule; if a real estate produced overlap,
  that would be useful too.
* **What breaks at 10,000 chargers.** Not the semantic layer. Episode clustering needs
  revisiting, and the dispatch ranking stops being a list and becomes a queue with
  capacity constraints.
* **Model choice.** The task is tool selection and argument construction, not generation
  volume. If a smaller model passes the same figure gate on the same questions, use it.
  The gate is the arbiter.
