# Architecture and design decisions

This document covers *why* the system is shaped the way it is, including the alternatives that
were rejected and the two bugs that changed the design. For the threat model behind the
guardrail policy, see [`guardrails.md`](guardrails.md).

---

## 1. Why a supervisor, and why only one

Three topologies were plausible for this workload:

| Topology | Why not |
| --- | --- |
| **Linear pipeline** (fetch → validate → report) | Correct for the happy path, and it cannot recover. A fetch that returns YAML where JSON was expected, a spec that fails to parse, a refused tool call — each needs a decision about what to do next, and a pipeline has no place to make one. |
| **Swarm / peer handoff** (each agent picks the next) | Every agent needs to know the whole workflow, and two agents that disagree about what has already run produce a loop that is very hard to see in a trace. |
| **Supervisor** ✅ | One place where "what happens next" is decided, one place to log it, one place to enforce preconditions and budgets. |

The topology is a star: `supervisor → worker → supervisor`. **Workers never route.** A worker
that picks the next node is a second supervisor, and the whole benefit of the pattern is having
exactly one. This is asserted in `tests/test_graph.py::test_workers_route_back_to_the_supervisor_only`.

## 2. The model proposes, code disposes

The supervisor asks the model for a `RouteDecision` (structured output — `next`, `reason`,
`confidence`), and then a deterministic ladder in `_apply_preconditions` decides whether that
move is legal.

Two failure modes show up immediately in practice when you act on router output directly:

1. **Re-dispatch.** The router sends work to a specialist that already succeeded, and the run
   loops until it hits a ceiling.
2. **Missing inputs.** The router sends work to the validator before anything has been fetched.

Both are cheap to check in code and expensive to fix with prompting. The prompt states the
preconditions, which reduces how often the ladder has to fire, but the ladder is what makes the
guarantee. There is a third benefit: because the ladder produces the same routing as a fallback,
**a router failure is not fatal** — if the model call raises, the supervisor falls through to
precondition ordering and the run completes.

Every hop is recorded with its `source`, so the routing trace distinguishes "the model chose
this" from "the model proposed something illegal and code corrected it". That distinction is the
whole audit story for the routing layer.

### Why structured output rather than parsing text

A router that returns prose has to be parsed, and a parser that fails has to decide what to do.
`with_structured_output` moves that failure to a place where it can be retried against a schema —
and schema rejection is the one failure a model can plausibly correct on a second attempt, which
is why `llm/client.py` retries specifically on it.

## 3. State design: typed fields, not a transcript

A common shape for supervisor systems is to put everything in the message list and let the
router re-read the transcript on every hop. That is expensive (the routing decision grows with
the conversation) and non-deterministic (the router's view of "has validation run?" depends on
how the validator phrased itself).

Here the message list carries narration only. Routing reads typed fields — `artifacts`,
`findings`, `completed` — that a worker either populated or did not. `_facts()` digests those
into a small dict; the full specification is never sent to the router, which would otherwise cost
more per routing decision than the actual evaluation.

Reducers matter: `findings` merges on `(rule_id, json_path)` so a re-run cannot inflate the
score, and `completed` / `route_log` / `guardrail_log` append.

## 4. Deterministic rules first, model second

The split is: **a rule answers it if a machine can answer it exactly; the model answers it if it
needs reading comprehension.**

Twelve rules cover structure, completeness, security and compatibility — presence of
`operationId`, uniqueness, documented error responses, response and request schemas, security
schemes declared but never applied, path parameters marked required, enums without defaults.
They are pure functions of the spec, so they cost nothing, always agree with themselves, and are
testable without a model.

The model is asked only about prose quality, which is a genuine judgement call. Its output comes
back through a Pydantic schema and is re-validated before entering state — an LLM response is
untrusted input until a schema has accepted it. Running the model over questions the rules
already settled would be slower, non-reproducible, and would produce a different score on every
run over the same document.

**The score is computed, only the narrative is generated.** An evaluation tool whose number moves
between runs over an identical input is not an evaluation tool.

## 5. Dual-mode: why the mock is not a stub

The replay model is not a canned-response fixture. Every agent renders the facts it wants the
model to reason over as JSON inside `<eval_context>` tags — which is better prompt design for the
live model anyway — and the replay model reads that same block and derives an answer from it.

The consequence is that mock mode exercises **real** routing conditions, **real** guardrail
triggers and **real** scoring. Recorded cassettes take priority when one matches, so a live run
can be captured and replayed verbatim. Token usage is estimated from text length, so the ledger
and the benchmark produce comparable figures in both modes.

Alternatives rejected: a `MagicMock` LLM (tests would pass while the graph was broken), and
`VCR`-style HTTP recording (requires a key to record anything at all, so the offline path could
never be the default).

## 6. Instrumentation via a ContextVar

LangGraph node functions receive `(state, config)`. Threading a ledger through every signature
would mean smuggling it through the state dict and serialising it on every hop. So the ledger is
ambient, held in a `ContextVar` and scoped by `use_ledger()`.

Guardrail evaluation gets its own span *kind*, which is what makes "what do the guardrails cost?"
a query against the ledger rather than an estimate.

## 7. Three bugs worth writing down

All are locked in by regression tests, and all were the kind that pass every test you would have
thought to write.

### `max()` on a `str`-based enum

`Tier` subclasses `str` so tiers serialise cleanly to YAML and JSON. Escalation was originally
`tier = max(tier, rule.escalate_to)`.

`max()` resolves through `__gt__`, which `str` provides. `"FORBIDDEN" > "SAFE"` is `False`
alphabetically — so **every escalation silently no-opped**. The policy file looked correct, the
rules matched correctly, `matched_rules` was populated correctly, and an SSRF fetch came back
`SENSITIVE / ALLOW_AUDITED`.

The fix is a single `escalate()` function and deliberately *no* comparison operators on `Tier`,
so there is exactly one way to raise a tier. The test asserts the trap directly —
`max(Tier.SAFE, Tier.FORBIDDEN) is Tier.SAFE` — so a future refactor back to `max()` fails loudly
instead of quietly disabling the policy.

### Benchmark arm ordering

The first version of the benchmark ran the entire control arm, then the entire treatment arm, and
reported that guardrails cost **+2.4 ms per run**. The direct microbenchmark of the same code
said 0.067 ms. A 34× discrepancy between two measurements of one thing means one of them is
measuring something else.

It was ordering: any drift over the benchmark's lifetime — page cache, allocator state, CPU
frequency, other load — landed entirely on whichever arm ran second and read as signal.
Interleaving the arms within each pair cancels it to first order, after which the A/B delta falls
inside the noise band and agrees with the direct measurement.

The reporting changed too. A benchmark that prints `-1.14 ms (-7.55%)` without saying that the
noise band is ±1.48 ms is not reporting a measurement, it is reporting a coin flip.

### A refused fetch retried until the budget ceiling

Found while wiring up CI, by reading a smoke-test failure rather than a test failure.

`_apply_preconditions` knew how to stop a worker that had already *succeeded* from being
re-dispatched, but had no notion of one that had already *failed*. On an unfetchable target —
an SSRF refusal, a path escaping the workspace, a 404 — the ladder saw `has_artifact=False`,
concluded the specification had not been fetched yet, and routed to the fetcher again. Twelve
times, until the iteration ceiling stopped it, paying for a routing call each time to rediscover
an identical failure.

The ceiling did its job, which is exactly why this was invisible: the run terminated, the tests
asserting "refused and still terminates" passed, and nothing was obviously wrong unless you looked
at the span counts. The cost was real though — $0.0265 against $0.0036 for the same doomed target
after the fix, on the cheap mock path; on live Opus 5 against a large document it is twelve times
the intended spend on a target that was never going to work.

The fix is one rule placed *ahead* of every other precondition, because all of them assume a
missing specification is still obtainable:

* a **policy refusal is terminal** and earns no retry at all — the refusal payload says in as many
  words that it will not succeed on retry, so honouring that is just believing our own contract;
* any other failure earns exactly **one** retry, since the tool layer already does its own
  exponential backoff for transient transport errors and a second graph-level attempt is where the
  useful retries end.

Two lessons worth keeping. A budget ceiling is a backstop, not a policy — if it is the thing
ending your runs, something upstream is not converging. And a test that asserts termination should
also assert *how fast*: `test_a_doomed_run_costs_a_fraction_of_a_real_one` is the check that would
have caught this.

## 8. What was deliberately left out

- **Persistent checkpointing.** A checkpoint exists here to survive a human-approval interrupt
  within one evaluation, not to outlive the process. `MemorySaver` is the right scope; swapping
  in a durable saver is a one-line change in `make_checkpointer()`.
- **Model-authored tool arguments.** The model chooses *which worker runs*; the worker chooses
  *which tool runs with which arguments*. Binding tools directly to the model would put a
  prompt-injected instruction in a fetched document one step away from the `webhook_url` of an
  outbound POST. Narrowing that surface is worth more than the flexibility it costs.
- **Parallel worker execution.** The three stages are strictly sequential — validation needs the
  fetch, reporting needs validation. Fan-out would add machinery for no wall-clock win.
- **A full OpenAPI 3.1 meta-schema.** ~1500 lines whose failure messages ("is not valid under any
  of the given schemas") are useless to an integrator. A compact structural schema catches shape
  errors; named rules catch substance and produce messages a human can act on.
- **Mandatory AgentOps.** Integrated behind `AGENTOPS_API_KEY` and entirely optional. A portfolio
  project that requires a second vendor account to run is a project nobody runs.

## 9. Known limitations

- The qualitative pass sees a digest of at most 40 operations. Very large specifications are
  validated in full by the deterministic rules but only partially reviewed for prose.
- `$ref` resolution is shallow: rules inspect references as written and do not chase them across
  documents, so a defect that only appears after full dereferencing is not caught.
- Scoring weights (critical 40 / major 15 / minor 4) are a defensible starting point, not a
  calibrated instrument. They are a single table in `state.py` and are meant to be tuned per
  organisation.
- Mock-mode token counts are estimated at ~4 characters per token. Figures are comparable between
  modes and exact only in live mode.
