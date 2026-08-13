"""Dependency-free credential reference policy shared by config and CLI."""

from __future__ import annotations

import re

ENV_REFERENCE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_CREDENTIAL_KEY_CONCEPTS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "api-key",
    "apikey",
    "private-key",
    "credential",
    "access-key",
    "dsn",
    "database-url",
    "redis-url",
    "mongodb-uri",
    "connection-string",
)
_CREDENTIAL_VALUE_SCHEMES = ("bearer ", "basic ", "token ")
_ENV_PATH_KEY_SUFFIXES = ("-file", "-path", "-dir", "-directory")
_ENV_NONCREDENTIAL_SUFFIXES = ("-max-tokens",)


def is_safe_credential_value(value: str) -> bool:
    """Return whether *value* is one reference with an optional public scheme."""
    stripped = value.strip()
    if ENV_REFERENCE_PATTERN.fullmatch(stripped):
        return True
    lowered = stripped.lower()
    for scheme in _CREDENTIAL_VALUE_SCHEMES:
        if lowered.startswith(scheme):
            return bool(ENV_REFERENCE_PATTERN.fullmatch(stripped[len(scheme) :]))
    return False


def is_credential_like_key(key: str) -> bool:
    """Return whether a header or environment key commonly carries a secret."""
    normalized = key.lower().replace("_", "-")
    return any(concept in normalized for concept in _CREDENTIAL_KEY_CONCEPTS)


def is_credential_like_env_key(key: str) -> bool:
    """Classify an env key, exempting paths and nonsecret capacity settings."""
    normalized = key.lower().replace("_", "-")
    if normalized.endswith(_ENV_PATH_KEY_SUFFIXES + _ENV_NONCREDENTIAL_SUFFIXES):
        return False
    return is_credential_like_key(normalized)
