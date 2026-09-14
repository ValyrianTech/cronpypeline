"""Tests for cronpypeline.template_safety — str.format template hardening."""

import pytest

from cronpypeline.template_safety import (
    is_sensitive_key,
    iter_template_fields,
    validate_template_fields,
)


class TestIterTemplateFields:
    """Tests for iter_template_fields."""

    def test_simple_fields(self):
        assert iter_template_fields("{a} {b}") == ["a", "b"]

    def test_ignores_escaped_braces(self):
        assert iter_template_fields("{{literal}} {a}") == ["a"]

    def test_no_fields(self):
        assert iter_template_fields("no placeholders here") == []

    def test_attribute_field_preserved_verbatim(self):
        assert iter_template_fields("{a.b}") == ["a.b"]

    def test_item_field_preserved_verbatim(self):
        assert iter_template_fields("{a[b]}") == ["a[b]"]


class TestValidateTemplateFields:
    """Tests for validate_template_fields."""

    def test_accepts_simple_identifier(self):
        assert validate_template_fields("{name}") == ["name"]

    def test_accepts_multiple_identifiers(self):
        assert validate_template_fields("{a} {b}") == ["a", "b"]

    def test_rejects_attribute_access(self):
        with pytest.raises(ValueError):
            validate_template_fields("{a.b}")

    def test_rejects_item_access(self):
        with pytest.raises(ValueError):
            validate_template_fields("{a[b]}")

    def test_rejects_positional_index(self):
        with pytest.raises(ValueError):
            validate_template_fields("{0}")

    def test_rejects_unbalanced_brace(self):
        with pytest.raises(ValueError):
            validate_template_fields("{")


class TestIsSensitiveKey:
    """Tests for is_sensitive_key."""

    def test_token(self):
        assert is_sensitive_key("github_token") is True

    def test_api_key(self):
        assert is_sensitive_key("api_key") is True

    def test_password_case_insensitive(self):
        assert is_sensitive_key("DB_PASSWORD") is True

    def test_secret(self):
        assert is_sensitive_key("client_secret") is True

    def test_auth_exact(self):
        assert is_sensitive_key("auth") is True

    def test_auth_prefix(self):
        assert is_sensitive_key("auth_header") is True

    def test_private_key(self):
        assert is_sensitive_key("private_key") is True

    def test_credentials(self):
        assert is_sensitive_key("credentials") is True

    def test_passwd(self):
        assert is_sensitive_key("db_passwd") is True

    def test_apikey(self):
        assert is_sensitive_key("apikey") is True

    def test_access_key(self):
        assert is_sensitive_key("access_key") is True

    def test_not_sensitive_test_cmd(self):
        assert is_sensitive_key("test_cmd") is False

    def test_not_sensitive_slug(self):
        assert is_sensitive_key("slug") is False

    def test_not_sensitive_coverage_threshold(self):
        assert is_sensitive_key("coverage_threshold") is False
