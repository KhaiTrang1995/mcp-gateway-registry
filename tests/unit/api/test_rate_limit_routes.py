"""Unit tests for registry/api/rate_limit_routes.py (admin CRUD for rate limits)."""

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

logger = logging.getLogger(__name__)


@pytest.fixture
def admin_user_context() -> dict[str, Any]:
    """Admin user context."""
    return {"username": "admin", "is_admin": True, "groups": ["mcp-registry-admin"]}


@pytest.fixture
def non_admin_user_context() -> dict[str, Any]:
    """Non-admin user context."""
    return {"username": "user", "is_admin": False, "groups": ["mcp-users"]}


@pytest.fixture
def mock_auth_admin(admin_user_context):
    """Override nginx_proxied_auth with an admin user."""
    from registry.auth.dependencies import nginx_proxied_auth
    from registry.main import app

    app.dependency_overrides[nginx_proxied_auth] = lambda: admin_user_context
    yield admin_user_context
    app.dependency_overrides.clear()


@pytest.fixture
def mock_auth_regular(non_admin_user_context):
    """Override nginx_proxied_auth with a non-admin user."""
    from registry.auth.dependencies import nginx_proxied_auth
    from registry.main import app

    app.dependency_overrides[nginx_proxied_auth] = lambda: non_admin_user_context
    yield non_admin_user_context
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    """TestClient over the real app."""
    from registry.main import app

    return TestClient(app)


@pytest.fixture
def mock_repository():
    """Patch the routes' repository singleton with an AsyncMock."""
    repo = AsyncMock()
    with patch("registry.api.rate_limit_routes._get_repository", return_value=repo):
        yield repo


@pytest.mark.unit
class TestPutRateLimit:
    """Tests for PUT /api/rate-limits/{id}."""

    def test_put_valid_definition(self, client, mock_auth_admin, mock_repository):
        """A valid definition whose body matches the URL id is stored."""
        from registry.rate_limiting.models import RateLimitDefinition

        definition = RateLimitDefinition(
            axis="caller", entity_type="group", name="developers", max_requests=5, window_seconds=60
        )
        mock_repository.upsert.return_value = definition

        body = {
            "axis": "caller",
            "entity_type": "group",
            "name": "developers",
            "max_requests": 5,
            "window_seconds": 60,
        }
        resp = client.put("/api/rate-limits/caller:group:developers:60", json=body)
        assert resp.status_code == 200
        assert resp.json()["max_requests"] == 5

    def test_put_url_id_mismatch_rejected(self, client, mock_auth_admin, mock_repository):
        """A body that builds a different _id than the URL id is a 400."""
        body = {
            "axis": "caller",
            "entity_type": "group",
            "name": "developers",
            "max_requests": 5,
            "window_seconds": 60,
        }
        # URL says window 30, body says 60 -> mismatch.
        resp = client.put("/api/rate-limits/caller:group:developers:30", json=body)
        assert resp.status_code == 400
        assert "does not match" in resp.json()["detail"]

    def test_put_invalid_axis_rejected(self, client, mock_auth_admin, mock_repository):
        """An invalid axis fails model validation with a 400."""
        body = {
            "axis": "sideways",
            "entity_type": "group",
            "name": "x",
            "max_requests": 5,
            "window_seconds": 60,
        }
        resp = client.put("/api/rate-limits/sideways:group:x:60", json=body)
        assert resp.status_code == 400

    def test_put_unenforced_entity_type_rejected(self, client, mock_auth_admin, mock_repository):
        """A modeled-but-unenforced entity type (mcp_tool) is rejected with a clear 400."""
        body = {
            "axis": "target",
            "entity_type": "mcp_tool",
            "name": "mcpgw:search",
            "max_requests": 5,
            "window_seconds": 60,
        }
        resp = client.put("/api/rate-limits/target:mcp_tool:mcpgw:search:60", json=body)
        assert resp.status_code == 400
        assert "not enforced" in resp.json()["detail"]

    def test_put_requires_admin(self, client, mock_auth_regular, mock_repository):
        """A non-admin gets 403."""
        body = {
            "axis": "caller",
            "entity_type": "group",
            "name": "developers",
            "max_requests": 5,
            "window_seconds": 60,
        }
        resp = client.put("/api/rate-limits/caller:group:developers:60", json=body)
        assert resp.status_code == 403


@pytest.mark.unit
class TestListAndDelete:
    """Tests for GET and DELETE."""

    def test_list_returns_definitions(self, client, mock_auth_admin, mock_repository):
        """List returns the repository's definitions."""
        from registry.rate_limiting.models import RateLimitDefinition

        mock_repository.list_all.return_value = [
            RateLimitDefinition(
                axis="target",
                entity_type="a2a_agent",
                name="booking",
                max_requests=2,
                window_seconds=60,
            )
        ]
        resp = client.get("/api/rate-limits")
        assert resp.status_code == 200
        assert len(resp.json()["definitions"]) == 1

    def test_delete_present(self, client, mock_auth_admin, mock_repository):
        """Deleting an existing definition returns deleted=true."""
        mock_repository.delete.return_value = True
        resp = client.request(
            "DELETE", "/api/rate-limits/caller:group:developers:60"
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

    def test_delete_absent_is_404(self, client, mock_auth_admin, mock_repository):
        """Deleting a missing definition returns 404."""
        mock_repository.delete.return_value = False
        resp = client.request("DELETE", "/api/rate-limits/caller:group:ghost:60")
        assert resp.status_code == 404
