"""Streamlit front end.

The UI exists for one reason the CLI cannot serve: a HIGH_RISK tool call
suspends the graph and needs a human to answer. Everything else here — the
routing trace, the guardrail table, the cost panel — is in service of making
that answer an informed one. An approval dialog that shows only "allow
purge_cache?" trains people to click yes.

State lives in ``st.session_state`` because Streamlit re-runs this script top to
bottom on every interaction, and a suspended graph has to survive that.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st
from langgraph.types import Command

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from doc_evaluator.config import Settings  # noqa: E402
from doc_evaluator.demo import SANDBOX_CALLS, build_approval_demo_graph  # noqa: E402
from doc_evaluator.graph import Evaluation  # noqa: E402
from doc_evaluator.guardrails.engine import GuardrailEngine  # noqa: E402
from doc_evaluator.tools.registry import interrupt_approver  # noqa: E402

FIXTURES = sorted((REPO_ROOT / "fixtures" / "specs").glob("*"))

TIER_BADGE = {
    "SAFE": "🟢 SAFE",
    "SENSITIVE": "🟡 SENSITIVE",
    "HIGH_RISK": "🟠 HIGH_RISK",
    "FORBIDDEN": "🔴 FORBIDDEN",
}

def md(text: str) -> str:
    """Escape ``$`` before handing text to ``st.markdown``.

    Streamlit treats ``$...$`` as LaTeX. Every JSON path in a finding starts
    with ``$``, so an unescaped report renders its most useful column as
    mangled equations.
    """
    return text.replace("$", "\\$")


SOURCE_NOTE = {
    "model": "router",
    "precondition": "overridden by a precondition",
    "budget": "forced by a budget ceiling",
    "completion": "run complete",
}

st.set_page_config(page_title="API Doc Evaluator", page_icon="🔍", layout="wide")


def get_settings() -> Settings:
    return Settings.from_env(
        mode=st.session_state.get("mode", "auto"),
        guardrails_enabled=st.session_state.get("guardrails_enabled", True),
    )


def reset_run() -> None:
    for key in ("run", "result", "target"):
        st.session_state.pop(key, None)


def reset_sandbox() -> None:
    for key in ("sandbox_graph", "sandbox_state", "sandbox_label"):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Configuration")

    probe = Settings.from_env()
    if probe.api_key:
        st.success("`ANTHROPIC_API_KEY` detected — live calls available.")
        mode_options = ["auto", "mock", "live"]
    else:
        st.info("No API key set. Running on replayed responses; everything still works.")
        mode_options = ["mock"]

    st.session_state["mode"] = st.selectbox(
        "Model mode",
        mode_options,
        help=(
            "mock replays deterministic responses derived from the same structured "
            "context the live model reads."
        ),
    )

    st.session_state["guardrails_enabled"] = st.toggle(
        "Guardrails enabled",
        value=True,
        help="Turning these off is the benchmark's control arm. Not a real operating mode.",
    )
    if not st.session_state["guardrails_enabled"]:
        st.warning("Policy evaluation is off. Every tool call will be allowed.")

    settings = get_settings()
    st.caption(f"model `{settings.model}` · mode `{settings.resolved_mode}`")

    st.divider()
    st.subheader("Policy")
    engine = GuardrailEngine(allowed_hosts=settings.allowed_hosts)
    for entry in engine.describe():
        with st.expander(f"{TIER_BADGE[entry['base_tier']]}  `{entry['tool']}`"):
            if entry["description"]:
                st.caption(entry["description"])
            if not entry["rules"]:
                st.caption("No escalation rules — risk does not vary with arguments.")
            for rule in entry["rules"]:
                st.markdown(
                    f"**`{rule['id']}`** → {TIER_BADGE[rule['escalate_to']]}  \n"
                    f"when `{rule['predicate']}({rule['arg']})`"
                )
                if rule["reason"]:
                    st.caption(rule["reason"])

    st.caption(f"Fetch allowlist: {', '.join(settings.allowed_hosts)}")


# ---------------------------------------------------------------------------
# Header and input
# ---------------------------------------------------------------------------

st.title("🔍 API Documentation Evaluator")
st.caption(
    "A supervisor routes work to three specialists — fetcher, validator, reporter — "
    "with every tool call passing through a tiered guardrail policy."
)

col_input, col_button = st.columns([5, 1])
with col_input:
    choices = [str(p.relative_to(REPO_ROOT)) for p in FIXTURES]
    target = st.selectbox(
        "OpenAPI document",
        choices + ["— enter a URL —"],
        help="Bundled fixtures run offline. A URL is fetched through the guardrail policy.",
    )
    if target == "— enter a URL —":
        target = st.text_input(
            "URL", placeholder="https://petstore3.swagger.io/api/v3/openapi.json"
        )

with col_button:
    st.write("")
    st.write("")
    start = st.button("Evaluate", type="primary", use_container_width=True)

def resolve_target(value: str) -> str:
    """Fixture selections are repo-relative; a typed URL is passed through as-is."""
    return value if "://" in value else str(REPO_ROOT / value)


if start and target:
    reset_run()
    st.session_state["target"] = target
    run = Evaluation(settings=get_settings(), approver=interrupt_approver)
    st.session_state["run"] = run
    with st.spinner("Evaluating…"):
        st.session_state["result"] = run.start(resolve_target(target))

result = st.session_state.get("result")
run = st.session_state.get("run")


# ---------------------------------------------------------------------------
# Guardrail sandbox — the only way to reach the HIGH_RISK path
# ---------------------------------------------------------------------------

with st.expander("🛡  Guardrail sandbox — make the agent attempt a risky call", expanded=False):
    st.caption(
        "The evaluation pipeline never calls a HIGH_RISK tool, by design. This runs a "
        "one-node graph through the same registry, policy and `interrupt()` cycle so the "
        "approval path is reachable. Tool implementations are stubbed — nothing is "
        "actually deleted or sent."
    )

    label = st.selectbox("Call to attempt", list(SANDBOX_CALLS), key="sandbox_choice")
    spec = SANDBOX_CALLS[label]
    st.code(f"{spec['tool']}({', '.join(f'{k}={v!r}' for k, v in spec['args'].items())})")

    if st.button("Attempt this call", key="sandbox_go"):
        reset_sandbox()
        graph = build_approval_demo_graph(get_settings(), spec["tool"], spec["args"])
        config = {"configurable": {"thread_id": f"sandbox-{abs(hash(label))}"}}
        graph.invoke({}, config)
        st.session_state["sandbox_graph"] = (graph, config)
        st.session_state["sandbox_label"] = label
        st.session_state["sandbox_state"] = graph.get_state(config)

    snapshot = st.session_state.get("sandbox_state")
    if snapshot is not None:
        graph, config = st.session_state["sandbox_graph"]
        interrupts = getattr(snapshot, "interrupts", ()) or ()

        if interrupts:
            payload = interrupts[0].value
            st.warning(
                f"**⏸ Paused — {TIER_BADGE.get(payload['risk_tier'], payload['risk_tier'])}**  \n"
                f"The agent wants to call `{payload['tool']}`."
            )
            st.info(md(payload["reason"]))
            st.json(payload.get("arguments", {}))

            yes, no = st.columns(2)
            if yes.button("✅ Allow", key="sandbox_yes", use_container_width=True):
                graph.invoke(Command(resume={"approved": True}), config)
                st.session_state["sandbox_state"] = graph.get_state(config)
                st.rerun()
            if no.button("🛑 Refuse", key="sandbox_no", use_container_width=True):
                graph.invoke(Command(resume={"approved": False}), config)
                st.session_state["sandbox_state"] = graph.get_state(config)
                st.rerun()
        else:
            values = snapshot.values
            tier = values.get("tier", "?")
            if values.get("outcome") == "executed":
                st.success(f"**{TIER_BADGE.get(tier, tier)} → executed** (implementation stubbed)")
            else:
                st.error(f"**{TIER_BADGE.get(tier, tier)} → {values.get('action')}**")
            detail = values.get("detail") or {}
            if detail.get("reason"):
                st.caption(md(detail["reason"]))
            if detail.get("matched_rules"):
                st.caption("Rules matched: " + ", ".join(f"`{r}`" for r in detail["matched_rules"]))
            with st.expander("What the agent received back"):
                st.json(detail.get("value") or {"error": detail.get("error")})


# ---------------------------------------------------------------------------
# Human approval — the reason this UI exists
# ---------------------------------------------------------------------------

if result is not None and result.interrupted and result.interrupt_payload:
    payload = result.interrupt_payload
    tier = payload.get("risk_tier", "HIGH_RISK")

    st.error(f"### ⏸ Paused for approval — {TIER_BADGE.get(tier, tier)}")
    st.markdown(f"The agent wants to call **`{payload['tool']}`**.")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Why this was escalated**")
        st.info(md(payload["reason"]))
        if payload.get("matched_rules"):
            st.caption("Rules matched: " + ", ".join(f"`{r}`" for r in payload["matched_rules"]))
    with right:
        st.markdown("**Exact arguments**")
        st.json(payload.get("arguments", {}), expanded=True)

    approve, deny = st.columns(2)
    if approve.button("✅ Allow this call", use_container_width=True):
        with st.spinner("Resuming…"):
            st.session_state["result"] = run.resume(True)
        st.rerun()
    if deny.button("🛑 Refuse", type="primary", use_container_width=True):
        with st.spinner("Resuming…"):
            st.session_state["result"] = run.resume(False)
        st.rerun()

    st.stop()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

if result is None:
    st.divider()
    st.markdown(
        "**Try:** `fixtures/specs/petstore.json` scores 100 · "
        "`fixtures/specs/legacy_billing.yaml` is a realistically bad spec that also "
        "carries a leaked credential · `fixtures/specs/broken_inventory.json` fails "
        "the structural contract."
    )
    st.stop()

summary = result.summary()
score = result.score or {}

if summary.get("halt_reason"):
    st.warning(f"**Partial evaluation** — {summary['halt_reason']}")

metrics = st.columns(5)
metrics[0].metric("Score", f"{score.get('score', 0)}/100", score.get("grade", "—"))
metrics[1].metric("Findings", summary["findings"])
metrics[2].metric("Cost", f"${summary['total_usd']:.5f}", f"{summary['total_tokens']:,} tokens")
metrics[3].metric("Wall time", f"{summary['elapsed_seconds']:.2f}s", f"{summary['hops']} hops")
metrics[4].metric(
    "Guardrail cost",
    f"{summary['guardrail_total_ms']:.3f} ms",
    f"{summary['guardrail_evaluations']} evaluations",
)

tab_report, tab_routing, tab_guardrails, tab_findings, tab_cost = st.tabs(
    ["Report", "Routing", "Guardrails", "Findings", "Cost & latency"]
)

with tab_report:
    if result.report:
        st.markdown(md(result.report))
        st.download_button(
            "Download report (markdown)",
            result.report,
            file_name="evaluation.md",
            mime="text/markdown",
        )
    else:
        st.info("No report was produced. The routing tab shows where the run stopped.")

with tab_routing:
    st.caption(
        "Every hop, and what produced it. A `precondition` source means the router "
        "proposed an illegal move and deterministic code corrected it."
    )
    for record in result.state.get("route_log") or []:
        note = SOURCE_NOTE.get(record.source, record.source)
        confidence = f" · confidence {record.confidence:.2f}" if record.confidence else ""
        icon = "🧭" if record.source == "model" else "🛡" if record.source == "budget" else "↩️"
        st.markdown(
            f"{icon} **{record.iteration}. → `{record.decision}`** *({note}{confidence})*"
        )
        st.caption(md(record.reason))

with tab_guardrails:
    events = result.state.get("guardrail_log") or []
    if not events:
        st.info("No tool calls were made.")
    else:
        st.caption(
            "Every tool call the run made, and what the policy decided. "
            "`ALLOW_AUDITED` means it ran and was logged; `BLOCK` means it never executed."
        )
        st.dataframe(
            [
                {
                    "Tool": e.tool,
                    "Tier": TIER_BADGE.get(e.tier, e.tier),
                    "Action": e.action,
                    "Rules": ", ".join(e.matched_rules) or "—",
                    "Approved": {True: "yes", False: "no"}.get(e.approved, "—"),
                    "Eval (µs)": round(e.eval_micros, 1),
                    "Why": e.reason,
                }
                for e in events
            ],
            use_container_width=True,
            hide_index=True,
        )
    redacted = (result.state.get("artifacts") or {}).get("redacted")
    if redacted:
        st.warning(
            "Redacted before the document reached the model: "
            + ", ".join(f"`{label}`" for label in redacted)
        )

with tab_findings:
    findings = result.findings
    if not findings:
        st.success("No findings. The specification passed every rule.")
    else:
        order = {"critical": 0, "major": 1, "minor": 2, "info": 3}
        st.dataframe(
            [
                {
                    "Severity": f.severity,
                    "Rule": f.rule_id,
                    "Category": f.category,
                    "Source": f.source,
                    "Location": f.json_path,
                    "Issue": f.message,
                }
                for f in sorted(findings, key=lambda f: (order[f.severity], f.rule_id))
            ],
            use_container_width=True,
            hide_index=True,
        )

with tab_cost:
    st.caption(
        f"Priced at the published rate for `{summary['model']}`. Mock mode estimates "
        "tokens from text length, so figures are comparable between modes but only "
        "exact in live mode."
    )
    st.dataframe(
        [
            {
                "Span": name,
                "Calls": int(row["calls"]),
                "Wall (ms)": round(row["wall_ms"], 3),
                "Tokens": int(row["tokens"]),
                "USD": round(row["usd"], 6),
            }
            for name, row in sorted(summary["by_name"].items(), key=lambda kv: -kv[1]["wall_ms"])
        ],
        use_container_width=True,
        hide_index=True,
    )
    st.download_button(
        "Download run ledger (JSON)",
        result.ledger.to_json(),
        file_name=f"ledger_{summary['run_id']}.json",
        mime="application/json",
    )
    with st.expander("Raw summary"):
        st.code(json.dumps({k: v for k, v in summary.items() if k != "by_name"}, indent=2))
