"""Template safety helpers for ``str.format``-based substitution.

``str.format`` allows attribute access (``{obj.attr}``) and item access
(``{mapping[key]}``), which would let a template read secrets out of a
variable mapping or arbitrary object internals. These helpers restrict
substitution to simple ``{name}`` identifier fields only.
"""

import string

# Substrings that indicate a config key is likely to carry a secret.
#
# NOTE: matching is deliberately substring-based, not whole-word. This
# over-matches benign keys (e.g. "tokenizer" and "authentication" are flagged
# because they contain "token" / "auth_"), which is an accepted, fail-closed
# trade-off: a false positive merely drops a harmless key from the template
# namespace, whereas a false negative could leak a real secret. Keeping the
# matcher a simple substring check errs on the side of caution and should not
# be "fixed" to whole-word matching.
_SENSITIVE_SUBSTRINGS = (
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


def iter_template_fields(template: str) -> list[str]:
    """Return the field names referenced by a ``str.format`` template."""
    return [field for _, field, _, _ in string.Formatter().parse(template) if field is not None]


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


def is_sensitive_key(name: str) -> bool:
    """Return True if a config key name likely carries a secret.

    Case-insensitive. Matching is deliberately substring-based (not whole-word),
    so it intentionally over-matches benign keys such as "tokenizer" or
    "authentication". This is a fail-closed trade-off: a false positive only
    drops a harmless key from the template namespace, while a false negative
    could leak a real secret. The simplicity of the substring check is kept on
    purpose and should not be "fixed" to whole-word matching.
    """
    lowered = name.lower()
    if lowered == "auth":
        return True
    return any(sub in lowered for sub in _SENSITIVE_SUBSTRINGS)
