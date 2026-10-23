"""Mock-tool domains (all simulated; no real systems).

:data:`DOMAINS` maps each domain name to its :class:`~baserate.traces.domains.base.Domain`.
"""

from __future__ import annotations

from baserate.traces.domains import customer_support
from baserate.traces.domains.base import Domain

DOMAINS: dict[str, Domain] = {d.name: d for d in (customer_support.DOMAIN,)}


def get_domain(name: str) -> Domain:
    """Look up a domain by name."""
    try:
        return DOMAINS[name]
    except KeyError as exc:
        raise KeyError(f"unknown domain {name!r}; known: {sorted(DOMAINS)}") from exc
