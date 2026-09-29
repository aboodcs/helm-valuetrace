"""
Pattern-based sensitive key detection and value redaction.

Redaction is ON by default.  Callers must explicitly pass ``redact=False``
or supply ``--no-redact`` on the CLI to disable it.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any

from helm_valuetrace.models import PathKey

REDACTED = "<redacted>"

DEFAULT_SENSITIVE_PATTERNS: list[str] = [
    "*password*",
    "*passwd*",
    "*secret*",
    "*token*",
    "*api_key*",
    "*apikey*",
    "*private_key*",
    "*privatekey*",
    "*access_key*",
    "*accesskey*",
    "*secret_key*",
    "*secretkey*",
    "*signing_key*",
    "*signingkey*",
    "*encryption_key*",
    "*encryptionkey*",
    "*enc_key*",
    "*ssh_key*",
    "*sshkey*",
    "*gpg_key*",
    "*gpgkey*",
    "*pgp_key*",
    "*pgpkey*",
    "*rsa_key*",
    "*rsakey*",
    "*tls_key*",
    "*tlskey*",
    "*jwt_key*",
    "*jwtkey*",
    "*hmac_key*",
    "*hmackey*",
    "*credential*",
    "*credentials*",
    "*auth*",
]

_SAFE_LEAF_WORDS: frozenset[str] = frozenset(
    {
        "author",
        "coauthor",
        "authority",
        "authorities",
        "oauth",
        "key",
        "keys",
        "monkey",
        "donkey",
        "turkey",
        "hockey",
        "keyboard",
        "turnkey",
        "keyring",
        "keynote",
        "keystone",
        "keyword",
        "keypad",
        "keyframe",
        "keyspace",
        "keyname",
        "keyformat",
        "keytype",
        "sorting",
    }
)

_SAFE_LEAF_SUFFIXES: tuple[str, ...] = (
    "count",
    "_count",
    "mode",
    "_mode",
)


def is_sensitive(
    key: str,
    patterns: list[str] = DEFAULT_SENSITIVE_PATTERNS,
) -> bool:
    """Return True if *key* matches any sensitive glob pattern.

    Only the leaf segment of a dotted key is examined so that a key like
    ``image.pullSecret`` is caught by ``*secret*`` without false-positives on
    parent segments.
    """
    key_lower = key.lower()
    leaf = re.sub(r"\[[0-9]+\]", "", key_lower.split(".")[-1])
    return _matches_name(leaf, patterns)


def _matches_name(leaf: str, patterns: list[str]) -> bool:
    """Match one complete mapping-key name, without interpreting its punctuation."""
    patterns = [p.lower() for p in patterns if not p.startswith("path:")]

    if leaf in _SAFE_LEAF_WORDS or ("." not in leaf and leaf.endswith(_SAFE_LEAF_SUFFIXES)):
        custom_patterns = [p for p in patterns if p not in DEFAULT_SENSITIVE_PATTERNS]
        return any(fnmatchcase(leaf, p) for p in custom_patterns)

    return any(fnmatchcase(leaf, p) for p in patterns)


def redaction_path(key: str | PathKey) -> PathKey:
    """Resolve escaped path syntax without confusing literal keys with containers."""
    if isinstance(key, tuple):
        return key
    from helm_valuetrace.exceptions import ValueTraceError
    from helm_valuetrace.parsers.set_parser import _parse_key_path

    try:
        return _parse_key_path(key)
    except ValueTraceError:
        return (key,)


def redact_value(
    value: Any,
    key: str | PathKey,
    patterns: list[str] = DEFAULT_SENSITIVE_PATTERNS,
) -> Any:
    """Return *REDACTED* when *key* is sensitive, otherwise return *value* unchanged."""
    path = redaction_path(key)
    custom_paths = [redaction_path(p[5:]) for p in patterns if p.startswith("path:")]
    if any(path[: len(p)] == p for p in custom_paths):
        return REDACTED
    if any(
        _matches_name(segment.lower(), patterns) for segment in path if isinstance(segment, str)
    ):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact_value(v, (*path, k), patterns) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, (*path, i), patterns) for i, v in enumerate(value)]
    return value
