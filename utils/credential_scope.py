"""Helpers for keeping API credentials bound to their intended endpoint.

An empty secret in an admin/API payload means "reuse the stored secret" only
when the provider and endpoint are unchanged.  Centralising that rule avoids a
credential configured for one service being sent to another service after an
endpoint edit or a portable configuration import.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit


LOCAL_LLM_PROVIDERS = frozenset({"local", "ollama", "custom"})


def normalize_endpoint(value: object) -> str:
    """Return a stable endpoint identity without user-info or a trailing slash."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/")
    netloc = parsed.netloc.rsplit("@", 1)[-1].lower()
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def credential_scope(
    config: Mapping | None,
    fallback: Mapping | None = None,
) -> tuple[str, str]:
    """Return ``(provider, endpoint)`` after applying optional inheritance."""
    values = config if isinstance(config, Mapping) else {}
    defaults = fallback if isinstance(fallback, Mapping) else {}
    provider = str(values.get("provider") or defaults.get("provider") or "").strip().lower()
    endpoint = normalize_endpoint(values.get("base_url") or defaults.get("base_url"))
    return provider, endpoint


def same_credential_scope(
    current: Mapping | None,
    candidate: Mapping | None,
    *,
    current_fallback: Mapping | None = None,
    candidate_fallback: Mapping | None = None,
) -> bool:
    """Whether a stored credential may safely be reused for ``candidate``."""
    return credential_scope(current, current_fallback) == credential_scope(
        candidate, candidate_fallback
    )


def resolve_scoped_api_key(
    candidate: Mapping | None,
    *,
    existing: Mapping | None = None,
    base: Mapping | None = None,
) -> str:
    """Resolve an explicit, existing, or base key without crossing scopes."""
    values = candidate if isinstance(candidate, Mapping) else {}
    explicit = str(values.get("api_key") or "")
    if explicit:
        return explicit

    previous = existing if isinstance(existing, Mapping) else {}
    old_key = str(previous.get("api_key") or "")
    if old_key and same_credential_scope(
        previous,
        values,
        current_fallback=base,
        candidate_fallback=base,
    ):
        return old_key

    defaults = base if isinstance(base, Mapping) else {}
    base_key = str(defaults.get("api_key") or "")
    if base_key and same_credential_scope(defaults, values, candidate_fallback=defaults):
        return base_key
    return ""


def llm_profile_is_usable(
    profile: Mapping | None,
    *,
    base: Mapping | None = None,
) -> bool:
    """Whether a model profile resolves to a safe callable HTTP endpoint."""
    values = profile if isinstance(profile, Mapping) else {}
    defaults = base if isinstance(base, Mapping) else {}
    if values.get("enabled", defaults.get("enabled", True)) is False:
        return False
    provider = str(values.get("provider") or defaults.get("provider") or "").strip().lower()
    endpoint = str(values.get("base_url") or defaults.get("base_url") or "").strip()
    model = str(values.get("model") or defaults.get("model") or "").strip()
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or not model
    ):
        return False
    return bool(provider in LOCAL_LLM_PROVIDERS or resolve_scoped_api_key(values, base=defaults))
