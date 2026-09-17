"""Tests for cronpypeline.template_safety — str.format template hardening."""

import pytest

from cronpypeline.template_safety import (
    flatten_target_config,
    is_sensitive_key,
    iter_template_fields,
    matches_credential_key,
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

    def test_nested_simple_field(self):
        assert iter_template_fields("{x:{y}}") == ["x", "y"]

    def test_nested_attribute_field_preserved_verbatim(self):
        assert iter_template_fields("{x:{y.w}}") == ["x", "y.w"]


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

    def test_rejects_nested_attribute_access(self):
        with pytest.raises(ValueError):
            validate_template_fields("{x:{y.w}}")

    def test_rejects_nested_item_access(self):
        with pytest.raises(ValueError):
            validate_template_fields("{x:{cfg[password]}}")

    def test_accepts_nested_simple_field(self):
        assert validate_template_fields("{x:{width}}") == ["x", "width"]

    def test_rejects_deeply_nested_attribute_access(self):
        with pytest.raises(ValueError):
            validate_template_fields("{x:{y:{z.w}}}")

    def test_accepts_multiple_nested_simple_fields(self):
        assert validate_template_fields("{value:{width}.{precision}f}") == [
            "value",
            "width",
            "precision",
        ]


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


class TestMatchesCredentialKey:
    """Tests for matches_credential_key (shared credential-key policy)."""

    def test_substring_mode_default(self):
        assert matches_credential_key("github_token") is True
        assert matches_credential_key("api_key") is True
        assert matches_credential_key("apikey") is True
        assert matches_credential_key("private_key") is True
        assert matches_credential_key("access_key") is True

    def test_substring_mode_exact_auth_true(self):
        assert matches_credential_key("auth", exact_auth=True) is True
        assert matches_credential_key("auth_header", exact_auth=True) is True

    def test_substring_mode_exact_auth_false(self):
        assert matches_credential_key("auth") is False
        assert matches_credential_key("auth", exact_auth=False) is False

    def test_whole_word_mode(self):
        assert matches_credential_key("key", whole_word=True) is True
        assert matches_credential_key("key-holder", whole_word=True) is True
        assert matches_credential_key("x-api-key", whole_word=True) is True
        assert matches_credential_key("monkey", whole_word=True) is False
        assert matches_credential_key("keyless", whole_word=True) is False

    def test_case_insensitivity(self):
        assert matches_credential_key("DB_PASSWORD") is True
        assert matches_credential_key("X-API-Key", whole_word=True) is True
        assert matches_credential_key("X-Monkey", whole_word=True) is False


class TestFlattenTargetConfig:
    """Tests for flatten_target_config."""

    def test_sensitive_keys_excluded(self):
        ctx: dict = {"target": "repo"}
        result = flatten_target_config(
            ctx,
            {"api_token": "SECRET", "client_secret": "SECRET", "db_password": "SECRET"},
        )
        assert result is ctx
        assert "api_token" not in ctx
        assert "client_secret" not in ctx
        assert "db_password" not in ctx
        assert ctx["target"] == "repo"

    def test_literal_target_config_key_excluded(self):
        ctx: dict = {}
        flatten_target_config(ctx, {"target_config": {"nested": "value"}})
        assert "target_config" not in ctx

    def test_non_sensitive_keys_flattened(self):
        ctx: dict = {}
        flatten_target_config(ctx, {"slug": "x", "test_cmd": "pytest"})
        assert ctx["slug"] == "x"
        assert ctx["test_cmd"] == "pytest"

    def test_existing_key_not_overwritten(self):
        ctx: dict = {"slug": "base"}
        flatten_target_config(ctx, {"slug": "from-config"})
        assert ctx["slug"] == "base"

    def test_none_target_config(self):
        ctx: dict = {"target": "repo"}
        result = flatten_target_config(ctx, None)
        assert result is ctx
        assert ctx == {"target": "repo"}
