"""Intra-admin safety guards for user-management operations.

These helpers protect the administrator population from two self-inflicted
lockout classes that admin authorization alone does not prevent:

- **Self-deletion.** An admin accidentally (or under coercion) deleting their
  own account.
- **Last-admin removal.** Deleting or demoting the final administrator, which
  would leave the deployment with no one able to manage users, groups, or
  scopes.

The admin population is derived from the same privileged-scope rules used by
the authorization layer (:mod:`registry.auth.privileged_constants` +
:func:`registry.services.scope_service._import_touches_privileged_scope`), so a
group is "admin-conferring" here for exactly the same reason a request bearing
it is treated as ``is_admin`` at request time. This keeps the guard aligned with
the admin-derivation rule instead of hard-coding a separate group list that can
drift.

Fail-closed: if the admin population cannot be determined (IdP/DB error, empty
result), the caller must treat the operation as removing the last admin and
DENY. A guard that can be silently skipped is equivalent to no guard.
"""

from __future__ import annotations

import logging

from ..services import scope_service
from ..services.scope_service import _import_touches_privileged_scope
from ..utils.iam_manager import get_iam_manager

logger = logging.getLogger(__name__)


class AdminSafetyError(Exception):
    """Raised when an admin-safety guard cannot be satisfied.

    Carries an HTTP-appropriate status code and a human-readable reason so the
    route layer can translate it into an HTTPException without re-deriving the
    failure mode.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _normalize(value: str | None) -> str:
    """Lower-case + strip a name for case-insensitive comparison.

    IdP usernames and group names are compared case-insensitively elsewhere in
    this module family (see the case-insensitive dedup in the user list/delete
    handlers), so we mirror that here to avoid a bypass where ``Admin`` and
    ``admin`` are treated as different principals.
    """
    return (value or "").strip().casefold()


def is_self_target(actor_username: str | None, target_username: str) -> bool:
    """Return True when the actor is operating on their own account.

    Comparison is case-insensitive and whitespace-insensitive. An empty or
    missing actor username never matches a target (fail closed: we cannot prove
    it is *not* self, but an unauthenticated actor should already have been
    rejected upstream, and refusing to match here avoids blocking legitimate
    deletes of a genuinely different user when the actor identity is unknown —
    the upstream admin gate remains the primary control).
    """
    actor = _normalize(actor_username)
    if not actor:
        return False
    return actor == _normalize(target_username)


async def resolve_admin_group_names() -> set[str]:
    """Return the set of group/scope names that confer admin, case-folded.

    A group confers admin when its scope definition is privileged under the
    shared authorization rules: the scope name (or any of its group mappings) is
    a privileged scope name, or its ui_permissions grant a mutating action to
    ``all`` resources. This is the same predicate the request-time admin check
    and the privileged-write guard use.

    Raises:
        AdminSafetyError: If the scope catalogue cannot be read. Fails closed so
            callers do not proceed on an incomplete admin picture.
    """
    try:
        groups = await scope_service.list_groups()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("admin_safety: failed to list scope groups: %s", exc)
        raise AdminSafetyError(
            status_code=503,
            detail="Unable to verify administrator population; operation refused",
        ) from exc

    if not isinstance(groups, dict) or groups.get("error"):
        logger.error("admin_safety: scope group listing unavailable or errored: %r", groups)
        raise AdminSafetyError(
            status_code=503,
            detail="Unable to verify administrator population; operation refused",
        )

    admin_names: set[str] = set()
    for scope_name, meta in groups.items():
        if not isinstance(scope_name, str) or not isinstance(meta, dict):
            continue
        group_mappings = meta.get("mappings") or []
        ui_permissions = meta.get("ui_scopes") or {}
        if _import_touches_privileged_scope(scope_name, group_mappings, ui_permissions):
            admin_names.add(_normalize(scope_name))
            for mapped in group_mappings:
                if isinstance(mapped, str):
                    admin_names.add(_normalize(mapped))

    # Fail closed on an empty result. A scope catalogue that lists groups but
    # matches ZERO admin-conferring scopes means our privileged-scope predicate
    # has drifted from the catalogue shape (or the catalogue is incomplete) — not
    # that the deployment genuinely has no admin groups. Proceeding would make
    # every user look non-admin and silently disable the last-admin guard, so we
    # refuse rather than derive a lockout decision from an unverifiable picture.
    if not admin_names:
        logger.error(
            "admin_safety: scope catalogue yielded ZERO admin-conferring groups "
            "(%d groups scanned); refusing to derive admin population",
            len(groups),
        )
        raise AdminSafetyError(
            status_code=503,
            detail="Unable to verify administrator population; operation refused",
        )

    logger.debug("admin_safety: resolved %d admin-conferring group names", len(admin_names))
    return admin_names


def groups_confer_admin(
    user_groups: list[str] | None,
    admin_group_names: set[str],
) -> bool:
    """Return True if any of the user's groups confers admin.

    Args:
        user_groups: The group names attached to the user (from the IdP listing).
        admin_group_names: Case-folded set from :func:`resolve_admin_group_names`.
    """
    for group in user_groups or []:
        if isinstance(group, str) and _normalize(group) in admin_group_names:
            return True
    return False


async def _m2m_admin_identities(admin_group_names: set[str]) -> list[frozenset[str]]:
    """Return one alias-set per admin M2M service account from ``idp_m2m_clients``.

    For non-Keycloak IdPs (Okta/Auth0/PingFederate) an M2M client's group
    membership lives ONLY in the MongoDB ``idp_m2m_clients`` collection, not in the
    IdP user listing. The user-management list handler merges this collection, so
    the admin count must too — otherwise an M2M-only admin is invisible and the
    last-admin guard both under-counts and (with the empty-set fail-closed) can
    wrongly refuse legitimate operations. Mirrors the merge in
    ``registry.api.management_routes.management_list_users``.

    Each admin is returned as a frozenset of ALL its identifiers (normalized
    ``name`` and ``client_id``). ``management_delete_user`` resolves an M2M client
    by ``{"$or": [{"client_id": ...}, {"name": ...}]}`` and the guard is called
    with the raw path param (either key), so the guard must recognise a delete by
    EITHER — but the two aliases are ONE admin, so they must count as one (a flat
    set of identifiers would double-count and re-open the last-admin fail-open).
    For Keycloak ``name == client_id`` so the set collapses to one element.

    Best-effort: a datastore error is logged and treated as "no M2M admins found"
    rather than raised, because the IdP listing is the primary source and the
    empty-population guard in :func:`list_admin_identities` still fails closed if
    the combined result is empty.

    Args:
        admin_group_names: Case-folded admin-conferring group set.

    Returns:
        One alias frozenset per M2M client whose groups confer admin.
    """
    from ..repositories.documentdb.client import get_documentdb_client

    m2m_admins: list[frozenset[str]] = []
    try:
        db = await get_documentdb_client()
        collection = db["idp_m2m_clients"]
        cursor = collection.find({})
        docs = await cursor.to_list(length=None)
    except Exception as exc:
        logger.warning(
            "admin_safety: could not read idp_m2m_clients for admin count "
            "(M2M admins not included): %s",
            exc,
        )
        return m2m_admins

    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        if not groups_confer_admin(doc.get("groups"), admin_group_names):
            continue
        aliases = {_normalize(key) for key in (doc.get("name"), doc.get("client_id")) if key}
        if aliases:
            m2m_admins.append(frozenset(aliases))

    logger.debug("admin_safety: %d M2M admin account(s) found", len(m2m_admins))
    return m2m_admins


async def list_admin_identities() -> list[frozenset[str]]:
    """Return one alias-set per current administrator.

    Each element is the set of case-folded identifiers by which one admin can be
    addressed: a single username for an IdP user, or ``{name, client_id}`` for an
    M2M service account (which a delete may target by either). Returning one entry
    PER ADMIN — rather than a flat set of identifiers — is what lets the guards
    both (a) recognise the target by any of its aliases and (b) count distinct
    admins correctly (a flat identifier set would count a two-alias M2M admin
    twice and re-open the last-admin fail-open).

    Cross-references the IdP/DB user listing AND the MongoDB ``idp_m2m_clients``
    store against the admin-conferring group set, so the population matches what
    the user-management list handler shows (M2M-only admins included). Fails closed
    on any error enumerating the primary IdP listing, and on an empty population.

    Raises:
        AdminSafetyError: If users/groups cannot be enumerated, or the resolved
            population is empty (treated as unverifiable, not a genuine zero).
    """
    admin_group_names = await resolve_admin_group_names()

    iam = get_iam_manager()
    try:
        users = await iam.list_users(max_results=1000, include_groups=True)
    except Exception as exc:
        logger.error("admin_safety: failed to list users for admin count: %s", exc)
        raise AdminSafetyError(
            status_code=503,
            detail="Unable to verify administrator population; operation refused",
        ) from exc

    admins: list[frozenset[str]] = []
    for user in users or []:
        if not isinstance(user, dict):
            continue
        username = user.get("username") or user.get("id")
        if not username:
            continue
        if groups_confer_admin(user.get("groups"), admin_group_names):
            admins.append(frozenset({_normalize(username)}))

    # Merge M2M admins (stored only in idp_m2m_clients for non-Keycloak IdPs) so
    # the count matches management_list_users and an M2M-only admin is not
    # invisible to the last-admin guard.
    admins.extend(await _m2m_admin_identities(admin_group_names))

    # Fail closed on an empty admin population. We already know at least one
    # admin-conferring group exists (resolve_admin_group_names raises otherwise),
    # so finding NO admin means the listing didn't surface group membership (e.g.
    # an IdP adapter that returned groupless users, or a truncated/empty listing)
    # rather than a genuine zero-admin deployment. Deriving "target is not an
    # admin, nothing to guard" from that would silently bypass the guard, so
    # refuse instead.
    if not admins:
        logger.error(
            "admin_safety: no administrator accounts found despite %d "
            "admin-conferring group(s); refusing (user listing likely incomplete)",
            len(admin_group_names),
        )
        raise AdminSafetyError(
            status_code=503,
            detail="Unable to verify administrator population; operation refused",
        )

    logger.debug("admin_safety: %d administrator account(s) currently present", len(admins))
    return admins


def _would_empty_admins(
    admins: list[frozenset[str]],
    target: str,
) -> bool:
    """Return True if removing ``target`` leaves zero administrators.

    ``target`` is a normalized identifier. An admin is "the target" if that
    identifier is one of its aliases; the removal empties the population only if
    NO OTHER admin (a different alias-set) remains. Counting distinct admins —
    not identifiers — is what prevents a two-alias M2M admin from masking itself.
    """
    is_target_admin = any(target in aliases for aliases in admins)
    if not is_target_admin:
        return False
    remaining = [aliases for aliases in admins if target not in aliases]
    return not remaining


async def assert_not_last_admin(target_username: str) -> None:
    """Refuse an operation that would remove the last administrator.

    Call this immediately before deleting an admin account, or before a group
    update that would strip a user of every admin-conferring group. It is a
    no-op when the target is not currently an admin, or when at least one other
    admin remains.

    Args:
        target_username: The account being removed/demoted.

    Raises:
        AdminSafetyError: If the target is the only remaining administrator, or
            the admin population cannot be determined (fail closed).
    """
    admins = await list_admin_identities()
    target = _normalize(target_username)

    if _would_empty_admins(admins, target):
        raise AdminSafetyError(
            status_code=409,
            detail=(
                "Refusing to remove the last administrator. Grant admin to "
                "another account before removing or demoting this one."
            ),
        )


async def would_remove_last_admin_via_groups(
    target_username: str,
    desired_groups: list[str] | None,
) -> None:
    """Refuse a group update that demotes the last administrator.

    Determines whether ``desired_groups`` still confers admin on the target. If
    the target is currently the sole admin and the new group set would strip
    their admin status, the update is refused.

    Args:
        target_username: The account whose groups are being replaced.
        desired_groups: The complete new group set the update would apply.

    Raises:
        AdminSafetyError: If applying ``desired_groups`` would leave zero
            administrators, or the admin population cannot be determined.
    """
    admin_group_names = await resolve_admin_group_names()

    # If the new group set still confers admin, the target stays an admin and no
    # lockout is possible from this update.
    if groups_confer_admin(desired_groups, admin_group_names):
        return

    admins = await list_admin_identities()
    target = _normalize(target_username)

    if _would_empty_admins(admins, target):
        raise AdminSafetyError(
            status_code=409,
            detail=(
                "Refusing to demote the last administrator. Grant admin to "
                "another account before removing admin from this one."
            ),
        )


async def desired_groups_grant_admin(desired_groups: list[str] | None) -> bool:
    """Return True if the desired group set would confer admin on a user.

    Used to emit a distinct audit event when a group update elevates an account
    to admin-tier.
    """
    admin_group_names = await resolve_admin_group_names()
    return groups_confer_admin(desired_groups, admin_group_names)
