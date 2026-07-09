"""Unit tests for configuration export functions.

Tests from LLD section 7.1 — validates export format correctness
for env, JSON, and tfvars outputs.
"""

import json

from registry.api.config_routes import (
    _export_as_env,
    _export_as_json,
    _export_as_tfvars,
)


class TestConfigExport:
    """Export function unit tests (Requirements 3.3, 3.4, 3.6, 3.7, 8.1)."""

    def test_export_env_masks_sensitive(self):
        """Verify _export_as_env(include_sensitive=False) masks sensitive values."""
        output = _export_as_env(include_sensitive=False)
        assert "SENSITIVE_VALUE_MASKED" in output
        # Sensitive fields should be commented out, not exposed
        assert "# SECRET_KEY=<SENSITIVE_VALUE_MASKED>" in output

    def test_export_env_includes_sensitive_when_requested(self):
        """Verify _export_as_env(include_sensitive=True) does not mask."""
        output = _export_as_env(include_sensitive=True)
        assert "SENSITIVE_VALUE_MASKED" not in output

    def test_export_json_valid_json(self):
        """Verify _export_as_json produces valid JSON with required keys."""
        output = _export_as_json(include_sensitive=False)
        parsed = json.loads(output)
        assert "_metadata" in parsed
        assert "configuration" in parsed
        assert "exported_at" in parsed["_metadata"]
        assert "registry_mode" in parsed["_metadata"]
        assert "includes_sensitive" in parsed["_metadata"]

    def test_export_tfvars_valid_syntax(self):
        """Verify _export_as_tfvars has no Python literals (None, True)."""
        output = _export_as_tfvars(include_sensitive=False)
        for line in output.splitlines():
            stripped = line.strip()
            # Skip comments and empty lines
            if stripped.startswith("#") or not stripped:
                continue
            # Should not contain Python-style True/False/None
            assert "None" not in stripped, f"Found Python 'None' in: {stripped}"
            assert "True" not in stripped, f"Found Python 'True' in: {stripped}"
            assert "False" not in stripped, f"Found Python 'False' in: {stripped}"

    def test_export_env_includes_resolved_ui_title(self):
        """exports surface the resolved title via the @property.

        CONFIG_GROUPS uses 'effective_ui_title' as the field name; this guards
        against a regression that switches it back to 'ui_title' and would emit
        the raw (often None) value instead of the deployment-mode-aware default.
        """
        output = _export_as_env(include_sensitive=False)
        assert "EFFECTIVE_UI_TITLE=" in output
        # The value must be a real string, not 'None' or empty.
        for line in output.splitlines():
            if line.startswith("EFFECTIVE_UI_TITLE="):
                value = line.split("=", 1)[1]
                assert value, "ui_title must always resolve to a non-empty string"
                assert value != "None"
                break
        else:
            raise AssertionError("EFFECTIVE_UI_TITLE not found in env export")

    def test_export_json_includes_resolved_ui_title(self):
        """JSON export under the deployment group has a resolved ui_title."""
        output = _export_as_json(include_sensitive=False)
        parsed = json.loads(output)
        deployment_group = parsed["configuration"].get("deployment", {})
        assert "effective_ui_title" in deployment_group
        assert deployment_group["effective_ui_title"]
        assert deployment_group["effective_ui_title"] != "None"


# ---------------------------------------------------------------------------
# Endpoint-level tests: deny-by-default sensitive export (blast radius)
# ---------------------------------------------------------------------------

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _admin_ctx() -> dict:
    return {
        "username": "admin",
        "is_admin": True,
        "auth_method": "session",
        "groups": ["mcp-registry-admin"],
        "scopes": [],
    }


@pytest.fixture
def export_client(mock_settings, _admin_ctx):
    """Admin-authenticated client for the /api/config/export endpoint."""
    from registry.api.config_routes import _check_rate_limit, _rate_limit_cache
    from registry.auth.dependencies import enhanced_auth
    from registry.main import app

    app.dependency_overrides[enhanced_auth] = lambda: _admin_ctx
    _rate_limit_cache.clear()
    _ = _check_rate_limit  # keep import used
    yield TestClient(app)
    app.dependency_overrides.clear()
    _rate_limit_cache.clear()


class TestSensitiveExportDenyByDefault:
    """The bulk sensitive export must be deny-by-default (SA-31 config prong)."""

    def test_include_sensitive_without_confirm_is_rejected(self, export_client):
        """include_sensitive=true alone no longer dumps secrets — it 400s."""
        resp = export_client.get("/api/config/export?format=env&include_sensitive=true")
        assert resp.status_code == 400
        assert "confirm" in resp.text.lower()

    def test_masked_export_is_default(self, export_client):
        """Default export masks sensitive values (no confirmation needed)."""
        resp = export_client.get("/api/config/export?format=env")
        assert resp.status_code == 200
        assert "SENSITIVE_VALUE_MASKED" in resp.text

    def test_include_sensitive_with_confirm_returns_unmasked(self, export_client):
        """Explicit double opt-in returns the unmasked export."""
        resp = export_client.get(
            "/api/config/export?format=env&include_sensitive=true&confirm_sensitive_export=true"
        )
        assert resp.status_code == 200
        assert "SENSITIVE_VALUE_MASKED" not in resp.text

    def test_confirm_without_include_sensitive_stays_masked(self, export_client):
        """confirm alone does not leak; masking still applies."""
        resp = export_client.get("/api/config/export?format=env&confirm_sensitive_export=true")
        assert resp.status_code == 200
        assert "SENSITIVE_VALUE_MASKED" in resp.text
