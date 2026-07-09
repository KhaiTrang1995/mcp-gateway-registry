"""Unit tests for registry/services/admin_safety.py.

Covers the intra-admin safety helpers: self-target detection, admin-group
resolution, admin-user counting, and the last-admin / demotion guards. Each
guard must fail closed (deny) when the admin population cannot be determined.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from registry.services import admin_safety
from registry.services.admin_safety import AdminSafetyError


class TestIsSelfTarget:
    """Tests for the self-deletion detection helper."""

    def test_matches_same_username(self):
        assert admin_safety.is_self_target("admin", "admin") is True

    def test_matches_case_insensitively(self):
        assert admin_safety.is_self_target("Admin", "admin") is True
        assert admin_safety.is_self_target("  admin ", "ADMIN") is True

    def test_different_user_is_not_self(self):
        assert admin_safety.is_self_target("admin", "someone-else") is False

    def test_empty_actor_never_matches(self):
        assert admin_safety.is_self_target("", "admin") is False
        assert admin_safety.is_self_target(None, "admin") is False


class TestGroupsConferAdmin:
    """Tests for the group->admin predicate."""

    def test_matches_admin_group(self):
        admins = {"mcp-registry-admin"}
        assert admin_safety.groups_confer_admin(["mcp-registry-admin"], admins) is True

    def test_case_insensitive(self):
        admins = {"mcp-registry-admin"}
        assert admin_safety.groups_confer_admin(["MCP-Registry-Admin"], admins) is True

    def test_no_match(self):
        admins = {"mcp-registry-admin"}
        assert admin_safety.groups_confer_admin(["some-group"], admins) is False

    def test_empty_groups(self):
        assert admin_safety.groups_confer_admin(None, {"mcp-registry-admin"}) is False
        assert admin_safety.groups_confer_admin([], {"mcp-registry-admin"}) is False


@pytest.mark.asyncio
class TestResolveAdminGroupNames:
    """Tests for admin-conferring group resolution from scope docs."""

    async def test_identifies_privileged_scope_name(self):
        # "readers" grants only scoped (non-"all") access, so it is NOT
        # admin-conferring. mcp-registry-admin is a privileged scope name.
        groups = {
            "mcp-registry-admin": {"ui_scopes": {}, "mappings": []},
            "readers": {"ui_scopes": {"list_service": ["some-server"]}, "mappings": []},
        }
        with patch(
            "registry.services.admin_safety.scope_service.list_groups",
            new=AsyncMock(return_value=groups),
        ):
            result = await admin_safety.resolve_admin_group_names()
        assert "mcp-registry-admin" in result
        # A scoped (non-"all") grant does not confer admin.
        assert "readers" not in result

    async def test_identifies_admin_via_ui_permissions(self):
        groups = {
            "custom-admins": {
                "ui_scopes": {"register_service": ["all"]},
                "mappings": ["idp-admin-group"],
            },
        }
        with patch(
            "registry.services.admin_safety.scope_service.list_groups",
            new=AsyncMock(return_value=groups),
        ):
            result = await admin_safety.resolve_admin_group_names()
        assert "custom-admins" in result
        # Mapped IdP group name is also treated as admin-conferring.
        assert "idp-admin-group" in result

    async def test_fails_closed_on_error_result(self):
        with patch(
            "registry.services.admin_safety.scope_service.list_groups",
            new=AsyncMock(return_value={"error": "boom", "groups": {}}),
        ):
            with pytest.raises(AdminSafetyError) as exc:
                await admin_safety.resolve_admin_group_names()
        assert exc.value.status_code == 503

    async def test_fails_closed_on_exception(self):
        with patch(
            "registry.services.admin_safety.scope_service.list_groups",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            with pytest.raises(AdminSafetyError):
                await admin_safety.resolve_admin_group_names()

    async def test_fails_closed_on_empty_admin_group_set(self):
        # Catalogue lists groups but NONE match the privileged-scope predicate
        # (predicate/catalogue drift). Must refuse rather than return an empty set
        # that would silently disable the last-admin guard.
        groups = {
            "readers": {"ui_scopes": {"list_service": ["some-server"]}, "mappings": []},
            "viewers": {"ui_scopes": {"list_service": ["another-server"]}, "mappings": []},
        }
        with patch(
            "registry.services.admin_safety.scope_service.list_groups",
            new=AsyncMock(return_value=groups),
        ):
            with pytest.raises(AdminSafetyError) as exc:
                await admin_safety.resolve_admin_group_names()
        assert exc.value.status_code == 503


def _iam_mock(users):
    mock = MagicMock()
    mock.list_users = AsyncMock(return_value=users)
    return mock


@pytest.mark.asyncio
class TestListAdminUsernames:
    """Tests for counting admin users."""

    async def test_counts_only_admin_users(self):
        users = [
            {"username": "admin1", "groups": ["mcp-registry-admin"]},
            {"username": "admin2", "groups": ["mcp-registry-admin"]},
            {"username": "regular", "groups": ["readers"]},
        ]
        with (
            patch(
                "registry.services.admin_safety.resolve_admin_group_names",
                new=AsyncMock(return_value={"mcp-registry-admin"}),
            ),
            patch(
                "registry.services.admin_safety.get_iam_manager",
                return_value=_iam_mock(users),
            ),
        ):
            admins = await admin_safety.list_admin_usernames()
        assert admins == {"admin1", "admin2"}

    async def test_fails_closed_when_users_unavailable(self):
        mock = MagicMock()
        mock.list_users = AsyncMock(side_effect=Exception("keycloak down"))
        with (
            patch(
                "registry.services.admin_safety.resolve_admin_group_names",
                new=AsyncMock(return_value={"mcp-registry-admin"}),
            ),
            patch(
                "registry.services.admin_safety.get_iam_manager",
                return_value=mock,
            ),
        ):
            with pytest.raises(AdminSafetyError) as exc:
                await admin_safety.list_admin_usernames()
        assert exc.value.status_code == 503

    async def test_fails_closed_when_no_admins_found(self):
        # Admin-conferring groups exist, but the user listing surfaces NO admin
        # (e.g. an IdP adapter that returned groupless users, or a truncated
        # listing). Deriving "no admins, nothing to guard" would bypass the
        # last-admin guard, so this must fail closed instead of returning set().
        users = [
            {"username": "regular1", "groups": ["readers"]},
            {"username": "regular2", "groups": []},
        ]
        with (
            patch(
                "registry.services.admin_safety.resolve_admin_group_names",
                new=AsyncMock(return_value={"mcp-registry-admin"}),
            ),
            patch(
                "registry.services.admin_safety.get_iam_manager",
                return_value=_iam_mock(users),
            ),
        ):
            with pytest.raises(AdminSafetyError) as exc:
                await admin_safety.list_admin_usernames()
        assert exc.value.status_code == 503


@pytest.mark.asyncio
class TestAssertNotLastAdmin:
    """Tests for the last-admin deletion guard."""

    async def test_allows_when_other_admins_remain(self):
        with patch(
            "registry.services.admin_safety.list_admin_usernames",
            new=AsyncMock(return_value={"admin1", "admin2"}),
        ):
            # Should not raise.
            await admin_safety.assert_not_last_admin("admin1")

    async def test_rejects_last_admin(self):
        with patch(
            "registry.services.admin_safety.list_admin_usernames",
            new=AsyncMock(return_value={"admin1"}),
        ):
            with pytest.raises(AdminSafetyError) as exc:
                await admin_safety.assert_not_last_admin("admin1")
        assert exc.value.status_code == 409

    async def test_noop_when_target_not_admin(self):
        with patch(
            "registry.services.admin_safety.list_admin_usernames",
            new=AsyncMock(return_value={"admin1"}),
        ):
            # Deleting a non-admin cannot empty the admin population.
            await admin_safety.assert_not_last_admin("regular-user")

    async def test_fails_closed_when_population_unknown(self):
        with patch(
            "registry.services.admin_safety.list_admin_usernames",
            new=AsyncMock(side_effect=AdminSafetyError(503, "unknown")),
        ):
            with pytest.raises(AdminSafetyError):
                await admin_safety.assert_not_last_admin("admin1")


@pytest.mark.asyncio
class TestWouldRemoveLastAdminViaGroups:
    """Tests for the last-admin demotion guard."""

    async def test_allows_when_new_groups_still_admin(self):
        with patch(
            "registry.services.admin_safety.resolve_admin_group_names",
            new=AsyncMock(return_value={"mcp-registry-admin"}),
        ):
            # Keeping the admin group means no demotion; must not raise.
            await admin_safety.would_remove_last_admin_via_groups("admin1", ["mcp-registry-admin"])

    async def test_rejects_demoting_last_admin(self):
        with (
            patch(
                "registry.services.admin_safety.resolve_admin_group_names",
                new=AsyncMock(return_value={"mcp-registry-admin"}),
            ),
            patch(
                "registry.services.admin_safety.list_admin_usernames",
                new=AsyncMock(return_value={"admin1"}),
            ),
        ):
            with pytest.raises(AdminSafetyError) as exc:
                await admin_safety.would_remove_last_admin_via_groups("admin1", ["readers"])
        assert exc.value.status_code == 409

    async def test_allows_demoting_when_other_admins_remain(self):
        with (
            patch(
                "registry.services.admin_safety.resolve_admin_group_names",
                new=AsyncMock(return_value={"mcp-registry-admin"}),
            ),
            patch(
                "registry.services.admin_safety.list_admin_usernames",
                new=AsyncMock(return_value={"admin1", "admin2"}),
            ),
        ):
            await admin_safety.would_remove_last_admin_via_groups("admin1", ["readers"])


@pytest.mark.asyncio
class TestDesiredGroupsGrantAdmin:
    """Tests for the admin-grant detection used by the audit hook."""

    async def test_true_when_granting_admin_group(self):
        with patch(
            "registry.services.admin_safety.resolve_admin_group_names",
            new=AsyncMock(return_value={"mcp-registry-admin"}),
        ):
            assert await admin_safety.desired_groups_grant_admin(["mcp-registry-admin"])

    async def test_false_for_non_admin_groups(self):
        with patch(
            "registry.services.admin_safety.resolve_admin_group_names",
            new=AsyncMock(return_value={"mcp-registry-admin"}),
        ):
            assert not await admin_safety.desired_groups_grant_admin(["readers"])
