# Guardrails: threat model and design

The retry/backoff and iteration-ceiling protections in this system are ordinary and every agent
loop needs them. This document is about the part that is a design decision: **a tiered policy
where a call's risk depends on its arguments, and where the scarce resource being protected is
the operator's attention.**

---

## What this is defending against

The agent reads documents it did not write. That single fact produces the threat model.

| # | Threat | Concretely | Control |
| --- | --- | --- | --- |
| 1 | **SSRF** | A URL — supplied by a user or suggested inside a fetched document — points at `169.254.169.254`, `localhost`, or an RFC1918 address. The agent becomes a proxy into the host's own network; the cloud metadata endpoint hands out IAM credentials. | `fetch.ssrf` → `FORBIDDEN` |
| 2 | **Path traversal** | A local "spec path" is `/etc/passwd` or `../../../root/.ssh/id_rsa`. Same class as SSRF, different namespace. | `fetch.path_escape` → `FORBIDDEN` |
| 3 | **Credential exfiltration** | A key leaks into an `example` block in a published spec, survives into the report, and an outbound POST sends it somewhere. | Redaction at the fetch boundary + `egress.secret_leak` → `FORBIDDEN` |
| 4 | **Prompt injection steering a side effect** | Text inside a fetched document instructs the agent to POST results to an attacker's URL. | `egress.unknown_destination` → `FORBIDDEN`; model never authors tool arguments |
| 5 | **Destructive over-reach** | A bounded delete becomes a wildcard delete. | `delete.unbounded_scope` → `FORBIDDEN`; bounded deletes → `HIGH_RISK` |
| 6 | **Runaway loops** | The supervisor never converges; tokens burn. | Iteration / token / wall-clock ceilings, degrading to a partial report |
| 7 | **Silent context poisoning** | Untrusted document text enters the model's context carrying secrets or instructions. | Redaction before the text reaches any prompt |

---

## The four tiers

| Tier | Action | The question it answers |
| --- | --- | --- |
| `SAFE` | Execute | Nothing to think about |
| `SENSITIVE` | Execute, audit | "What did the agent decide to read?" — answerable after the fact |
| `HIGH_RISK` | Suspend via `interrupt()`, wait for a human | A human can make a better decision here than the policy |
| `FORBIDDEN` | Never execute; return a structured refusal | No human can make a better decision here than the policy |

The line between the last two is the interesting one, and it is not "how dangerous is this".

## Design decision 1 — Risk is a property of the call

A per-tool risk table cannot express that `fetch_document` is routine for an allowlisted host and
catastrophic for `169.254.169.254`. It has to pick one tier for the tool, and either choice is
wrong: `SAFE` lets the SSRF through, `HIGH_RISK` puts a dialog in front of every ordinary fetch
until the operator stops reading them.

So the policy declares a base tier plus **predicates over the arguments** that escalate it.
Predicates are pure functions returning `(matched, detail)`; the `detail` string is what appears
in the audit log and the approval dialog, so every escalation can explain itself in the operator's
words rather than as a rule id.

## Design decision 2 — Escalation is monotonic

A rule can raise a tier. Nothing can lower one.

This is what makes the policy readable. You can look at any single rule in isolation and know it
cannot be the reason something dangerous got through — the worst it can do is be too strict. In
an allow/deny list, ordering decides the outcome, so understanding any rule means understanding
every rule above it.

It is enforced by a single `escalate()` function and by `Tier` deliberately having no comparison
operators. (See `architecture.md` §7 for the bug that motivated that.)

## Design decision 3 — Deny by default

An undeclared tool gets `SENSITIVE`, not `SAFE`. Adding a tool to the codebase without thinking
about its risk should be noisy, not free. The decision says so explicitly —
`policy.undeclared_tool` appears in `matched_rules` — so the audit log distinguishes "no rules
applied" from "this tool is not in the policy at all".

Policy errors fail at **load** time, not call time: an unknown predicate name, an invalid tier, or
an incomplete rule raises when the engine is constructed. A policy that silently degrades to
"allow" because of a typo is worse than no policy.

## Design decision 4 — A confirmation prompt is a scarce resource

Every dialog a person clicks through without reading makes the next one less effective. That is
the actual budget being managed, and it is why some genuinely dangerous operations are `FORBIDDEN`
rather than `HIGH_RISK`:

- **A wildcard delete is blocked, not confirmed.** This is the single call where an approval
  prompt is a liability rather than a control: the operator sees a plausible-looking request,
  approves it, and the recoverable case and the catastrophic case looked identical in the dialog.
- **An outbound POST to a destination that came from the fetched document is blocked.** Asking a
  human "may the agent send this to `https://attacker.example/collect`?" is asking them to
  validate a decision made by untrusted input. There is no version of that question a person can
  answer well.
- **A body carrying credential material is blocked.** Nobody can audit a 4 KB report for a leaked
  key inside a modal.

Conversely, a bounded delete and a POST to an allowlisted destination *are* `HIGH_RISK`, because
a human genuinely holds context the policy does not — whether this artifact still matters, whether
this report is ready to send.

The corresponding rule for the approval dialog itself: it shows the **matched rules, the reason
text, and the exact arguments**. An approval prompt that says only "allow `purge_cache`?" trains
people to click yes.

## Design decision 5 — Refusals are data, not exceptions

A blocked call returns a structured payload:

```json
{
  "status": "refused",
  "tool": "send_external_report",
  "risk_tier": "FORBIDDEN",
  "reason": "[egress.secret_leak] payload carries credential material (anthropic_key) — …",
  "matched_rules": ["egress.secret_leak"],
  "guidance": "This call was refused by policy and will not succeed on retry. Continue the
               evaluation without it, and note the omission in the report."
}
```

Raising would either crash the graph or be swallowed by a `try/except` somewhere upstream. The
agent needs to *read* why it was refused so it can route around it, and the run needs to finish
with the refusal recorded rather than disappear.

The `guidance` field exists specifically to stop retry loops. Without it, a model that gets an
error naturally tries again.

## Design decision 6 — The default approver denies

`deny_all_approver` is the default, not `approve_all`.

An unattended run — CI, a scheduled evaluation, a test — has nobody to ask. Defaulting to approve
would mean `HIGH_RISK` silently degrades to `SAFE` exactly when no human is watching, which
inverts the entire point of the tier. The graph's default is `interrupt_approver`, which suspends
and *waits*; if nothing resumes it, the call never happens.

## Design decision 7 — Redaction shares one table with egress

`redaction.py` owns the credential pattern set. `scrub()` uses it on the way in; the
`payload_contains_secret` predicate uses it on the way out. One table means "don't show the model
this" and "don't let the model send this out" cannot drift apart.

Placeholders are explicitly tolerated. `your_api_key`, `changeme`, `<token>`, `xxxxxxxx` and any
run of near-identical characters are recognised as masks. This is a deliberate accuracy tradeoff:
every public OpenAPI example contains placeholder credentials, and a check that fires on all of
them is a check the operator learns to ignore. **False positives are the failure mode that kills
a guardrail.**

## Design decision 8 — Enforcement has one chokepoint

Workers hold a `ToolRegistry`, not function references. `registry.call()` evaluates policy,
handles approval, and only then looks up the implementation. There is no second path, so "can a
tool run without policy evaluation?" has a structural answer.

Two consequences worth noting:

- **The tool implementation is never looked up for a blocked call.** Policy runs before the
  function is resolved, which is what lets the UI sandbox stub implementations without changing
  any decision.
- **Defence in depth is still applied.** `purge_cache` independently refuses to delete outside its
  own directory, even though `delete.unbounded_scope` and `fetch.path_escape` should have caught
  it first. A policy is a statement of intent, and intent is not a substitute for the function
  refusing.

## Design decision 9 — No credential-shaped literals in the repository

Exercising redaction and the egress block needs strings that look like real keys.
`src/doc_evaluator/samples.py` assembles every one of them from fragments at import time
(`"sk-" + "ant-api03-…"`) rather than writing them out.

This is not superstition. GitHub's push protection rejected an earlier version of this branch
over a Slack token in a test file — a value invented for the test, but a scanner cannot tell a
fake from a live key, and that is the correct behaviour for a scanner. The runtime values still
match the detection patterns, so nothing about the tests is weakened; only the source text
changes.

The general point is the same one the placeholder-tolerance rule makes from the other side: a
project about not leaking credentials should not model checking them in, and a control that
routinely fires on things everybody knows are harmless stops being a control.

---

## What this does *not* defend against

Stating the limits is part of the design.

- **A malicious operator.** Anyone who can approve a `HIGH_RISK` call can do the damage that call
  does. The tiers manage mistakes and manipulation, not authorised abuse.
- **Prompt injection that changes the *evaluation*.** Text in a fetched document can influence
  what the model says about that document — inflating a score, suppressing a finding. The
  deterministic rules are immune (they never see prose as an instruction) and the score is
  computed from findings, so the blast radius is the narrative and the qualitative findings. It
  is a real gap, and the mitigation is the split itself: anything that matters is decided by code.
- **Exfiltration through allowlisted channels.** If a webhook destination is allowlisted, the
  policy checks the body for credentials but cannot judge whether the *content* should be leaving.
- **A compromised dependency.** `httpx`, `jsonschema` and LangGraph run in-process with full
  privileges. Sandboxing the tool layer is the honest fix and is out of scope here.
- **DNS rebinding.** `private_network_url` classifies the hostname as written. A name that
  resolves to a public address at check time and a private one at connect time defeats it; pinning
  the resolved IP through to the socket is the real fix.
- **Resource exhaustion below the ceilings.** The token and wall-clock ceilings bound a run, not a
  fleet of runs.

## Extending the policy

Add a predicate to `guardrails/predicates.py` (signature `(value, ctx, **params) -> (bool, str)`),
register it in `REGISTRY`, and reference it from `policy.yaml`:

```yaml
my_tool:
  tier: SENSITIVE
  rules:
    - id: my_tool.dangerous_shape
      arg: payload
      predicate: matches_regex
      pattern: "DROP\\s+TABLE"
      escalate_to: FORBIDDEN
      reason: Explain here why a human could not usefully approve this.
```

The `reason` field is not decoration — it is what the operator reads in the approval dialog and
what a reviewer reads six months later. A rule that cannot explain itself in a sentence is
usually a rule that has not been thought through.
