"""The guardrail policy engine.

``evaluate()`` is the single chokepoint every tool call passes through. It is
deliberately pure: it takes a tool name and arguments and returns a decision.
It does not execute anything, does not prompt, and does not raise on a blocked
call — enforcement lives in ``tools/registry.py`` and the human-approval hop
lives in the graph. Keeping the decision separable from the enforcement is what
makes the whole policy unit-testable without a running graph.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .predicates import PredicateContext, resolve

POLICY_PATH = Path(__file__).with_name("policy.yaml")


def _default_workspace() -> Path:
    """The project directory. Local reads outside it are blocked by policy."""
    from ..config import REPO_ROOT

    return REPO_ROOT


class Tier(StrEnum):
    """Risk tiers, ordered by :attr:`rank`.

    Deliberately *not* given comparison operators. ``StrEnum`` members compare
    as strings, so Python answers ``Tier.FORBIDDEN > Tier.SAFE`` alphabetically —
    ``"FORBIDDEN" > "SAFE"`` is ``False``, which would make every escalation
    silently no-op while the policy file still looked correct. Use
    :func:`escalate` instead; there is exactly one way to raise a tier.
    """

    SAFE = "SAFE"
    SENSITIVE = "SENSITIVE"
    HIGH_RISK = "HIGH_RISK"
    FORBIDDEN = "FORBIDDEN"

    @property
    def rank(self) -> int:
        return _TIER_RANK[self]


_TIER_RANK: dict[Tier, int] = {
    Tier.SAFE: 0,
    Tier.SENSITIVE: 1,
    Tier.HIGH_RISK: 2,
    Tier.FORBIDDEN: 3,
}


def escalate(current: Tier, candidate: Tier) -> Tier:
    """Return the higher-risk of two tiers. The only way a tier ever changes."""
    return candidate if candidate.rank > current.rank else current


class Action(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_AUDITED = "ALLOW_AUDITED"
    CONFIRM = "CONFIRM"
    BLOCK = "BLOCK"


TIER_ACTION: dict[Tier, Action] = {
    Tier.SAFE: Action.ALLOW,
    Tier.SENSITIVE: Action.ALLOW_AUDITED,
    Tier.HIGH_RISK: Action.CONFIRM,
    Tier.FORBIDDEN: Action.BLOCK,
}


@dataclass(frozen=True)
class Rule:
    id: str
    arg: str
    predicate: str
    escalate_to: Tier
    reason: str = ""
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    tier: Tier
    description: str = ""
    rules: tuple[Rule, ...] = ()


@dataclass(frozen=True)
class Decision:
    """The verdict on one prospective tool call."""

    tool: str
    tier: Tier
    action: Action
    reason: str
    matched_rules: tuple[str, ...] = ()
    args_digest: str = ""
    eval_micros: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.action in (Action.ALLOW, Action.ALLOW_AUDITED)

    @property
    def needs_approval(self) -> bool:
        return self.action is Action.CONFIRM

    @property
    def blocked(self) -> bool:
        return self.action is Action.BLOCK

    def refusal_payload(self) -> dict[str, Any]:
        """Structured refusal handed back to the agent instead of a tool result.

        Returned as data rather than raised as an exception on purpose: the
        agent needs to *read* why it was refused so it can choose a different
        action, and an exception would either crash the graph or be swallowed.
        """
        return {
            "status": "refused",
            "tool": self.tool,
            "risk_tier": self.tier.value,
            "reason": self.reason,
            "matched_rules": list(self.matched_rules),
            "guidance": (
                "This call was refused by policy and will not succeed on retry. "
                "Continue the evaluation without it, and note the omission in the report."
            ),
        }


class PolicyError(ValueError):
    """Malformed policy file. Raised at load time, never at call time."""


def digest_args(args: dict[str, Any]) -> str:
    """Stable short digest of a call's arguments, safe to put in an audit log."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = repr(sorted(args))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _parse_tier(raw: Any, where: str) -> Tier:
    try:
        return Tier(str(raw).upper())
    except ValueError as exc:
        raise PolicyError(
            f"{where}: {raw!r} is not a valid tier ({', '.join(t.value for t in Tier)})"
        ) from exc


def load_policy(path: Path | str | None = None) -> dict[str, Any]:
    """Load and validate the policy manifest.

    Every failure mode here is a load-time error. A policy that silently
    degrades to "allow" because of a typo is worse than no policy at all.
    """
    path = Path(path) if path else POLICY_PATH
    raw = yaml.safe_load(path.read_text()) or {}
    default_tier = _parse_tier(raw.get("default_tier", "SENSITIVE"), "default_tier")

    tools: dict[str, ToolPolicy] = {}
    for name, spec in (raw.get("tools") or {}).items():
        spec = spec or {}
        rules: list[Rule] = []
        for entry in spec.get("rules") or []:
            missing = {"id", "arg", "predicate", "escalate_to"} - set(entry)
            if missing:
                raise PolicyError(f"tool {name!r} rule is missing {sorted(missing)}")
            resolve(entry["predicate"])  # fail fast on an unknown predicate name
            known = {"id", "arg", "predicate", "escalate_to", "reason"}
            rules.append(
                Rule(
                    id=entry["id"],
                    arg=entry["arg"],
                    predicate=entry["predicate"],
                    escalate_to=_parse_tier(entry["escalate_to"], f"tool {name!r} rule"),
                    reason=str(entry.get("reason", "")).strip(),
                    params={k: v for k, v in entry.items() if k not in known},
                )
            )
        tools[name] = ToolPolicy(
            name=name,
            tier=_parse_tier(spec.get("tier", default_tier.value), f"tool {name!r}"),
            description=str(spec.get("description", "")).strip(),
            rules=tuple(rules),
        )

    return {"version": raw.get("version", 1), "default_tier": default_tier, "tools": tools}


class GuardrailEngine:
    """Evaluates prospective tool calls against the policy."""

    def __init__(
        self,
        policy_path: Path | str | None = None,
        allowed_hosts: tuple[str, ...] = (),
        enabled: bool = True,
        workspace_root: Path | str | None = None,
    ) -> None:
        self.policy = load_policy(policy_path)
        self.tools: dict[str, ToolPolicy] = self.policy["tools"]
        self.default_tier: Tier = self.policy["default_tier"]
        self.enabled = enabled
        self.ctx = PredicateContext(
            allowed_hosts=tuple(h.lower() for h in allowed_hosts),
            workspace_root=Path(workspace_root) if workspace_root else _default_workspace(),
        )

    def tier_for(self, tool: str) -> Tier:
        policy = self.tools.get(tool)
        return policy.tier if policy else self.default_tier

    def describe(self) -> list[dict[str, Any]]:
        """Policy summary for the UI and the docs."""
        return [
            {
                "tool": p.name,
                "base_tier": p.tier.value,
                "description": p.description,
                "rules": [
                    {
                        "id": r.id,
                        "arg": r.arg,
                        "predicate": r.predicate,
                        "escalate_to": r.escalate_to.value,
                        "reason": r.reason,
                    }
                    for r in p.rules
                ],
            }
            for p in sorted(self.tools.values(), key=lambda p: (-p.tier.rank, p.name))
        ]

    def evaluate(self, tool: str, args: dict[str, Any] | None = None) -> Decision:
        args = args or {}
        digest = digest_args(args)
        start = time.perf_counter()

        if not self.enabled:
            # The benchmark's control arm. Explicitly labelled so a decision
            # from a disabled engine can never be mistaken for an approval.
            return Decision(
                tool=tool,
                tier=Tier.SAFE,
                action=Action.ALLOW,
                reason="guardrails disabled",
                args_digest=digest,
                eval_micros=(time.perf_counter() - start) * 1e6,
            )

        policy = self.tools.get(tool)
        if policy is None:
            return Decision(
                tool=tool,
                tier=self.default_tier,
                action=TIER_ACTION[self.default_tier],
                reason=(
                    f"{tool!r} is not declared in the policy; applying the "
                    f"default tier {self.default_tier.value}"
                ),
                matched_rules=("policy.undeclared_tool",),
                args_digest=digest,
                eval_micros=(time.perf_counter() - start) * 1e6,
            )

        tier = policy.tier
        matched: list[str] = []
        reasons: list[str] = []

        for rule in policy.rules:
            if rule.arg not in args:
                continue
            predicate = resolve(rule.predicate)
            hit, detail = predicate(args[rule.arg], self.ctx, **rule.params)
            if not hit:
                continue
            matched.append(rule.id)
            # Monotonic: a rule can only raise the tier, never lower it.
            tier = escalate(tier, rule.escalate_to)
            reasons.append(f"[{rule.id}] {detail}" + (f" — {rule.reason}" if rule.reason else ""))

        reason = " ; ".join(reasons) if reasons else (
            f"no escalation rules matched; base tier {policy.tier.value}"
        )
        return Decision(
            tool=tool,
            tier=tier,
            action=TIER_ACTION[tier],
            reason=reason,
            matched_rules=tuple(matched),
            args_digest=digest,
            eval_micros=(time.perf_counter() - start) * 1e6,
        )
