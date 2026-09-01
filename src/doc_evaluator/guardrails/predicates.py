"""Argument-level risk predicates.

The central design choice of this guardrail layer: **risk is a property of the
call, not of the tool**. ``fetch_document("https://petstore3.swagger.io/...")``
and ``fetch_document("http://169.254.169.254/latest/meta-data/")`` are the same
tool and must not carry the same tier. A per-tool risk table cannot express
that; a predicate over the arguments can.

Each predicate takes the resolved argument value plus an evaluation context and
returns ``(matched, detail)``. Predicates are pure and fast — they sit on the
hot path of every tool call, and the benchmark measures them.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .redaction import find_secrets

PredicateResult = tuple[bool, str]


@dataclass(frozen=True)
class PredicateContext:
    allowed_hosts: tuple[str, ...] = ()
    workspace_root: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Wildcard / unbounded-scope markers for destructive operations.
_BULK_MARKERS = ("*", "%", "?", "..", "**")
_BULK_EXACT = {"", "/", ".", "all", "everything", ".*", "^.*$"}

_INTERNAL_SUFFIXES = (".internal", ".local", ".localdomain", ".cluster.local")
_INTERNAL_NAMES = {"localhost", "metadata.google.internal", "instance-data"}


def _host_of(value: Any) -> str:
    parsed = urlparse(str(value))
    return (parsed.hostname or "").lower()


def _matches_host(host: str, allowed: str) -> bool:
    """Exact host match, or a subdomain of an allowed parent domain."""
    return host == allowed or host.endswith("." + allowed)


def is_local_path(value: Any) -> bool:
    """A local path, not a network URL. ``file://`` counts as local."""
    text = str(value or "")
    return "://" not in text or text.startswith("file://")


def url_not_in_allowlist(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    """Host-based allowlist check.

    Local paths are explicitly *not* a match here. A filesystem path has no
    host, so "not on the host allowlist" is a category error — it would fire on
    every offline run and produce a reason that reads as nonsense. Local reads
    have their own rules below, which is where their real risk lives.
    """
    if is_local_path(value):
        return False, "local path — evaluated by the filesystem rules instead"
    host = _host_of(value)
    if not host:
        return True, f"no resolvable host in {value!r}"
    if any(_matches_host(host, allowed) for allowed in ctx.allowed_hosts):
        return False, f"{host} is allowlisted"
    return True, f"{host} is not on the fetch allowlist"


def private_network_url(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    """Catch SSRF targets: loopback, RFC1918, link-local (cloud metadata), internal TLDs."""
    host = _host_of(value)
    if not host:
        return False, "no host to classify"
    if host in _INTERNAL_NAMES or host.endswith(_INTERNAL_SUFFIXES):
        return True, f"{host} resolves inside the private namespace"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False, f"{host} is a public hostname"
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True, f"{host} is a private / link-local address"
    return False, f"{host} is a public address"


def insecure_scheme(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    scheme = urlparse(str(value)).scheme.lower()
    if scheme in ("http", "ftp", "file", "gopher"):
        return True, f"{scheme}:// carries no transport protection"
    return False, f"{scheme}:// is acceptable"


def local_filesystem_read(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    if is_local_path(value):
        return True, f"reads {str(value)!r} from the local filesystem"
    return False, "not a local read"


def path_escapes_workspace(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    """Catch a read that resolves outside the project directory.

    ``../../../../etc/passwd`` and ``/root/.ssh/id_rsa`` are the same class of
    request as the SSRF case: the agent reaching for host state that has nothing
    to do with evaluating an API document. Blocked rather than confirmable for
    the same reason — there is no legitimate version of it to approve.
    """
    if not is_local_path(value):
        return False, "not a local path"
    root = ctx.workspace_root
    if root is None:
        return False, "no workspace root configured"
    raw = str(value or "").removeprefix("file://")
    try:
        resolved = Path(raw).resolve()
        resolved.relative_to(Path(root).resolve())
    except ValueError:
        return True, f"{raw!r} resolves outside the workspace"
    except OSError:
        return True, f"{raw!r} could not be resolved safely"
    return False, f"{raw!r} is inside the workspace"


def bulk_scope(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    """True when a destructive argument has no bounded scope."""
    text = "" if value is None else str(value).strip()
    if text.lower() in _BULK_EXACT:
        return True, f"scope {text!r} is unbounded"
    if any(marker in text for marker in _BULK_MARKERS):
        return True, f"scope {text!r} contains a wildcard"
    return False, f"scope {text!r} is bounded"


def payload_contains_secret(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    hits = find_secrets(str(value or ""))
    if hits:
        kinds = sorted({name for name, _ in hits})
        return True, f"payload carries credential material ({', '.join(kinds)})"
    return False, "no credential material in payload"


def matches_regex(
    value: Any, ctx: PredicateContext, pattern: str = "", **_: Any
) -> PredicateResult:
    if not pattern:
        return False, "no pattern configured"
    hit = re.search(pattern, str(value or ""))
    return (True, f"matched /{pattern}/") if hit else (False, f"no match for /{pattern}/")


def arg_equals(
    value: Any, ctx: PredicateContext, expected: Any = None, **_: Any
) -> PredicateResult:
    equal = str(value) == str(expected)
    return equal, f"value {'==' if equal else '!='} {expected!r}"


def exceeds_length(
    value: Any, ctx: PredicateContext, limit: int = 100_000, **_: Any
) -> PredicateResult:
    size = len(str(value or ""))
    if size > limit:
        return True, f"payload is {size} chars (limit {limit})"
    return False, f"payload is {size} chars"


def always(value: Any, ctx: PredicateContext, **_: Any) -> PredicateResult:
    return True, "unconditional rule"


REGISTRY: dict[str, Callable[..., PredicateResult]] = {
    "url_not_in_allowlist": url_not_in_allowlist,
    "local_filesystem_read": local_filesystem_read,
    "path_escapes_workspace": path_escapes_workspace,
    "private_network_url": private_network_url,
    "insecure_scheme": insecure_scheme,
    "bulk_scope": bulk_scope,
    "payload_contains_secret": payload_contains_secret,
    "matches_regex": matches_regex,
    "arg_equals": arg_equals,
    "exceeds_length": exceeds_length,
    "always": always,
}


class UnknownPredicateError(KeyError):
    """Raised at policy-load time so a typo fails fast instead of silently allowing."""


def resolve(name: str) -> Callable[..., PredicateResult]:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise UnknownPredicateError(
            f"unknown predicate {name!r}; available: {', '.join(sorted(REGISTRY))}"
        ) from exc
