"""Template safety helpers for ``str.format``-based substitution.

``str.format`` allows attribute access (``{obj.attr}``) and item access
(``{mapping[key]}``), which would let a template read secrets out of a
variable mapping or arbitrary object internals. These helpers restrict
substitution to simple ``{name}`` identifier fields only.
"""

import re
import string
from typing import Any

# Canonical lowercase credential fragments used for substring matching. These
# are the single source of truth for the "what looks like a secret" policy and
# are shared by actions.py (via matches_credential_key(whole_word=True) for
# header matching; CREDENTIAL_WORDS is applied internally by that helper) and
# webui/app.py (via matches_credential_key for config-key redaction).
#
# NOTE: substring matching is deliberately over-broad, not whole-word. This
# over-matches benign keys (e.g. "tokenizer" and "authentication" are flagged
# because they contain "token" / "auth_"), which is an accepted, fail-closed
# trade-off: a false positive merely drops a harmless key from the template
# namespace, whereas a false negative could leak a real secret. Keeping the
# matcher a simple substring check errs on the side of caution and should not
# be "fixed" to whole-word matching.
CREDENTIAL_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "_auth",
    "auth_",
)

# Backward-compatible alias for the substring fragments, retained so any
# external references to the historical private name keep working.
_SENSITIVE_SUBSTRINGS = CREDENTIAL_SUBSTRINGS

# Canonical lowercase credential keywords used for whole-word matching (e.g.
# HTTP header names such as ``x-api-key``). Unlike CREDENTIAL_SUBSTRINGS, these
# are matched on word boundaries so a keyword appearing only as a substring of
# a larger word (e.g. ``key`` inside ``monkey``) is not flagged.
CREDENTIAL_WORDS = ("auth", "token", "key", "secret", "credential", "password")


def iter_template_fields(template: str) -> list[str]:
    """Return the field names referenced by a ``str.format`` template.

    ``str.format`` also substitutes nested replacement fields inside a format
    spec (e.g. ``{x:{y.w}}`` performs ``y.w`` attribute access), so this recurses
    into the ``format_spec`` element of each parsed tuple to surface those fields
    too.
    """
    fields: list[str] = []
    for _, field, format_spec, _ in string.Formatter().parse(template):
        if field is not None:
            fields.append(field)
        if format_spec:
            fields.extend(iter_template_fields(format_spec))
    return fields


def validate_template_fields(template: str) -> list[str]:
    """Validate that a template only uses simple ``{name}`` substitution.

    Rejects attribute access (``.``), item access (``[``/``]``), and any other
    non-identifier field name by raising :class:`ValueError`.
    Returns the list of validated field names.

    :raises ValueError: if a field uses attribute/item access or is not a valid
        identifier/template syntax.
    """
    try:
        fields = iter_template_fields(template)
    except ValueError as e:
        raise ValueError(f"Unsupported/invalid template field: {e}") from e
    for field in fields:
        if "." in field or "[" in field or "]" in field or not field.isidentifier():
            raise ValueError(f"Unsupported/invalid template field: {field!r}")
    return fields


def matches_credential_key(name: str, *, whole_word: bool = False, exact_auth: bool = False) -> bool:
    """Return True if ``name`` looks like a credential/secret key.

    Case-insensitive. Two documented matching modes are supported:

    * **Substring mode** (``whole_word=False``, the default): the lowercased
      name is sensitive if it is exactly ``"auth"`` and ``exact_auth`` is True,
      or if any fragment in :data:`CREDENTIAL_SUBSTRINGS` appears as a
      substring. This over-matches benign keys (e.g. "tokenizer",
      "authentication") as a fail-closed trade-off.

    * **Whole-word mode** (``whole_word=True``): the lowercased name is
      sensitive if any keyword in :data:`CREDENTIAL_WORDS` appears as a whole
      word, matched with ``\\b`` word boundaries. This is intended for HTTP
      header names, where ``key`` in ``monkey`` should not be flagged.

    :param name: Key or header name to inspect.
    :param whole_word: When True, use word-boundary matching against
        :data:`CREDENTIAL_WORDS`; when False, use substring matching against
        :data:`CREDENTIAL_SUBSTRINGS`.
    :param exact_auth: In substring mode only, treat a bare ``"auth"`` name as
        sensitive. (``is_sensitive_key`` passes ``exact_auth=True``; the webui
        passes ``exact_auth=False`` so the plain key ``"auth"`` is kept.)
    :returns: ``True`` if the name likely carries a secret, else ``False``.
    """
    lowered = name.lower()
    if whole_word:
        return any(
            re.search(r"\b" + re.escape(keyword) + r"\b", lowered)
            for keyword in CREDENTIAL_WORDS
        )
    if lowered == "auth" and exact_auth:
        return True
    return any(sub in lowered for sub in CREDENTIAL_SUBSTRINGS)


def is_sensitive_key(name: str) -> bool:
    """Return True if a config key name likely carries a secret.

    Case-insensitive. Matching is deliberately substring-based (not whole-word),
    so it intentionally over-matches benign keys such as "tokenizer" or
    "authentication". This is a fail-closed trade-off: a false positive only
    drops a harmless key from the template namespace, while a false negative
    could leak a real secret. The simplicity of the substring check is kept on
    purpose and should not be "fixed" to whole-word matching.
    """
    return matches_credential_key(name, exact_auth=True)


def flatten_target_config(ctx: dict[str, Any], target_config: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten ``target_config`` keys into ``ctx``, excluding sensitive keys.

    Iterates ``target_config`` (treated as empty when ``None``), skipping the
    literal ``"target_config"`` key and any key for which
    :func:`is_sensitive_key` returns True (so secrets never reach marker
    filenames, directories, or logs). Only non-conflicting keys are set:
    ``ctx[k] = v`` is performed only when ``k`` is not already present in
    ``ctx``, preserving any pre-existing base variables.

    :param ctx: Base context dict to flatten into (mutated in place).
    :param target_config: Per-target configuration dict, or ``None``.
    :returns: The ``ctx`` dict, for convenience.
    """
    for k, v in (target_config or {}).items():
        if k == "target_config" or is_sensitive_key(str(k)):
            continue
        if k not in ctx:
            ctx[k] = v
    return ctx
