#!/usr/bin/env python3
"""End-to-end smoke checks against the installed package.

The unit suite imports modules directly, which means it cannot catch a broken
entry point — an earlier commit on this branch shipped a dangling import in
``cli.py`` that no test touched because no test imports it. So this script
drives the console script the way the README tells a user to, in a subprocess,
and asserts on what comes back.

The assertions are on values, not just exit codes. A run that exits 0 while
scoring a clean specification 40/100 is a regression, and CI should say so.

Run it from the repository root:

    python scripts/smoke.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPECS = REPO_ROOT / "fixtures" / "specs"

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))
        failures.append(label)


def run(args: list[str], expect_zero: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args, cwd=REPO_ROOT, capture_output=True, text=True, timeout=600
    )
    if expect_zero and proc.returncode != 0:
        print(f"  FAIL  command exited {proc.returncode}: {' '.join(args)}")
        print((proc.stdout + proc.stderr)[-2000:])
        failures.append(" ".join(args))
    return proc


def cli(*args: str) -> list[str]:
    """Invoke the installed console script, proving the packaging works."""
    return ["doc-evaluator", *args]


def summary_json(stdout: str) -> dict:
    """Pull the trailing ``--json`` summary out of the CLI's human-readable output."""
    start = stdout.rfind("\n{")
    if start == -1:
        raise ValueError("no JSON object found in CLI output")
    return json.loads(stdout[start:])


def evaluate(target: str) -> dict:
    """Run one evaluation and return its summary. ``target`` may be a fixture name."""
    resolved = str(SPECS / target) if (SPECS / target).is_file() else target
    proc = run(cli("--target", resolved, "--mock", "--approve", "no", "--json"))
    return summary_json(proc.stdout)


# ---------------------------------------------------------------------------

print("\nsmoke: a clean specification scores full marks")
clean = evaluate("petstore.json")
check("petstore scores 100", clean.get("score") == 100, f"got {clean.get('score')}")
check("petstore grades A", clean.get("grade") == "A", f"got {clean.get('grade')}")
check("petstore has no findings", clean.get("findings") == 0, f"got {clean.get('findings')}")
check("run is not interrupted", clean.get("interrupted") is False)
check("run is priced", clean.get("total_usd", 0) > 0, f"got {clean.get('total_usd')}")
check(
    "guardrails evaluated every tool call",
    clean.get("guardrail_evaluations") == 3,
    f"got {clean.get('guardrail_evaluations')}",
)

print("\nsmoke: a flawed specification is caught")
flawed = evaluate("legacy_billing.yaml")
check("legacy_billing scores 0", flawed.get("score") == 0, f"got {flawed.get('score')}")
check("legacy_billing grades F", flawed.get("grade") == "F", f"got {flawed.get('grade')}")
check(
    "legacy_billing reports 23 findings",
    flawed.get("findings") == 23,
    f"got {flawed.get('findings')}",
)

print("\nsmoke: a structurally broken specification still produces a report")
broken = evaluate("broken_inventory.json")
check("broken_inventory grades F", broken.get("grade") == "F", f"got {broken.get('grade')}")
check("broken_inventory has findings", broken.get("findings", 0) > 0)

print("\nsmoke: redaction fires on the document carrying a credential")
report_path = Path(tempfile.mkdtemp()) / "report.md"
run(
    cli(
        "--target", str(SPECS / "legacy_billing.yaml"),
        "--mock", "--approve", "no", "--report", str(report_path),
    )
)
report = report_path.read_text() if report_path.is_file() else ""
check("a report was written", bool(report))
check("the report records that redaction happened", "Redacted before analysis" in report)
check(
    "the credential never reaches the report",
    "SYNTHETIC0EXAMPLE0NOT0A0REAL0KEY" not in report,
)

print("\nsmoke: the guardrail policy blocks what it claims to block")
demo = run(cli("--demo-risky-tool")).stdout
for tool, rule in (
    ("SSRF / cloud metadata", "fetch.ssrf"),
    ("wildcard delete", "delete.unbounded_scope"),
    ("credential egress", "egress.secret_leak"),
    ("unknown egress destination", "egress.unknown_destination"),
):
    line = next((ln for ln in demo.splitlines() if rule in ln), "")
    check(f"{tool} is BLOCKed", "BLOCK" in line, f"matched line: {line.strip()[:90]!r}")
check("an allowlisted read stays frictionless", "SAFE       ALLOW" in demo)

print("\nsmoke: the policy is loadable and complete")
policy = json.loads(run(cli("--show-policy")).stdout)
check("policy declares 5 tools", len(policy) == 5, f"got {len(policy)}")
check(
    "both side-effecting tools are HIGH_RISK",
    {p["tool"] for p in policy if p["base_tier"] == "HIGH_RISK"}
    == {"purge_cache", "send_external_report"},
)

print("\nsmoke: the benchmark still runs")
with tempfile.TemporaryDirectory() as tmp:
    bench = run(
        [
            sys.executable, "benchmarks/guardrail_overhead.py",
            "--repeats", "2", "--warmup", "1", "--policy-iterations", "30",
            "--results-dir", tmp,
        ]
    )
    check("benchmark reports a per-evaluation cost", "µs per policy evaluation" in bench.stdout)
    written = sorted(Path(tmp).glob("*"))
    check("benchmark wrote its results", len(written) == 2, f"got {[p.name for p in written]}")

print("\nsmoke: an unfetchable target fails fast instead of looping")
doomed = evaluate("../../etc/passwd")
check("a refused fetch does not produce a report", doomed.get("score") is None)
check(
    "a refused fetch stops within a few hops",
    doomed.get("hops", 99) <= 3,
    f"got {doomed.get('hops')} hops",
)
check(
    "a doomed run costs a fraction of a real one",
    doomed.get("total_usd", 1) < clean.get("total_usd", 0),
    f"doomed ${doomed.get('total_usd')} vs clean ${clean.get('total_usd')}",
)


print("\nsmoke: the CLI refuses impossible invocations")
check("--live without a key exits 2", run(cli("--live", "--target", "x"), False).returncode == 2)
check(
    "--mock with --live exits 2",
    run(cli("--mock", "--live", "--target", "x"), False).returncode == 2,
)
check("no target exits 2", run(cli(), False).returncode == 2)

# ---------------------------------------------------------------------------

print()
if failures:
    print(f"FAILED — {len(failures)} of {checks} checks did not pass:")
    for name in failures:
        print(f"  - {name}")
    raise SystemExit(1)
print(f"all {checks} smoke checks passed")
