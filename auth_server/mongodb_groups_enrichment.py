"""DocumentDB/MongoDB Groups Enrichment for M2M Tokens.

This module provides functionality to enrich M2M tokens with groups from DocumentDB/MongoDB
when the IdP token has empty groups claim. This solves the authorization problem
for M2M clients across all identity providers (Keycloak, Okta, Entra).

Works with both:
- AWS DocumentDB (with IAM auth or username/password)
- MongoDB Community Edition (local or cloud)
"""

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s,p%(process)s,{%(filename)s:%(lineno)d},%(levelname)s,%(message)s",
)

logger = logging.getLogger(__name__)


_mongodb_database: AsyncIOMotorDatabase | None = None


# A record whose `enabled` field is anything other than boolean True has been
# disabled by an operator (or synced as inactive from the upstream IdP) and MUST
# NOT contribute groups/scopes to a token. Records that predate the `enabled`
# field (field absent) are treated as active for backward compatibility. Both
# registry create paths and the Okta/Auth0 sync paths write ``enabled``
# explicitly as a boolean, so an active record always carries ``enabled: True``
# and a revoked one carries ``enabled: False``.
#
# The query filter is a pre-filter only; ``_is_record_enabled`` is the
# authoritative, fail-closed re-check applied to whatever the query returns.
_ENABLED_FILTER: dict[str, Any] = {"enabled": {"$ne": False}}


def _is_record_enabled(doc: dict[str, Any]) -> bool:
    """Return whether an enrichment record is active (not operator-disabled).

    Fail-closed: a record is active only when its ``enabled`` field is absent
    (backward compatibility with records created before the flag existed) or is
    the boolean ``True``. Any other present value -- ``False``, ``None``, ``0``,
    an empty string, the string ``"false"``, or any non-boolean -- is treated as
    disabled and ignored. MongoDB does not enforce BSON types, so a record with
    a non-boolean ``enabled`` (however it got written) must never grant groups.

    Args:
        doc: The MongoDB document for an M2M client or user-group mapping.

    Returns:
        ``True`` if the record may contribute groups, ``False`` if it has been
        disabled (or carries a non-``True`` ``enabled`` value) and must be
        ignored.
    """
    if "enabled" not in doc:
        return True
    return doc["enabled"] is True


async def _get_mongodb() -> AsyncIOMotorDatabase:
    """Get MongoDB/DocumentDB database connection singleton.

    This uses the same connection logic as the registry to ensure compatibility
    with both MongoDB Community Edition and AWS DocumentDB.

    Returns:
        MongoDB/DocumentDB database instance

    Raises:
        ValueError: If database connection parameters not configured
    """
    global _mongodb_client, _mongodb_database

    if _mongodb_database is not None:
        return _mongodb_database

    try:
        # Use the registry's DocumentDB client for compatibility
        # This handles both MongoDB CE and AWS DocumentDB with proper auth mechanisms
        import sys
        from pathlib import Path

        # Add registry path to sys.path if not already there
        registry_path = Path(__file__).parent.parent / "registry"
        if str(registry_path) not in sys.path:
            sys.path.insert(0, str(registry_path.parent))

        from registry.repositories.documentdb.client import get_documentdb_client

        _mongodb_database = await get_documentdb_client()
        logger.info("✓ Connected to DocumentDB/MongoDB for groups enrichment")

        return _mongodb_database

    except Exception as e:
        logger.error(f"Failed to connect to DocumentDB/MongoDB: {e}")
        raise ValueError(f"Database connection failed: {e}")


async def enrich_groups_from_mongodb(
    client_id: str,
    current_groups: list[str],
) -> list[str]:
    """Enrich groups from DocumentDB/MongoDB if current groups are empty.

    This function checks if an M2M client has groups defined in the database
    and returns them if the current groups list is empty. This provides
    a fallback authorization mechanism for M2M tokens.

    Works with both AWS DocumentDB and MongoDB Community Edition.

    Args:
        client_id: Client ID from the JWT token
        current_groups: Current groups from JWT token

    Returns:
        Enriched groups list (either from MongoDB or original)
    """
    # If groups already exist in token (non-empty array), use them
    if current_groups:
        # Count only: group names are organizational PII.
        logger.debug(f"Client {client_id} has {len(current_groups)} groups in token")
        return current_groups

    logger.info(f"Client {client_id} has no groups in token, querying database")

    # Try to fetch groups from DocumentDB/MongoDB
    try:
        db = await _get_mongodb()
        collection = db["idp_m2m_clients"]

        # Exclude operator-disabled records at the query level so a revoked M2M
        # client cannot regain groups/scopes via the enrichment fallback.
        doc = await collection.find_one({"client_id": client_id, **_ENABLED_FILTER})

        if doc and not _is_record_enabled(doc):
            # Defense in depth: never trust groups from a disabled record even
            # if the query filter is ever loosened.
            logger.info(f"Client {client_id} record is disabled; skipping enrichment")
            doc = None

        if doc:
            db_groups = doc.get("groups", [])
            if db_groups:
                logger.info(
                    f"Enriched {len(db_groups)} groups for client {client_id} from database"
                )
                return db_groups
            else:
                logger.debug(f"Client {client_id} found in database but has no groups")
        else:
            logger.debug(f"Client {client_id} not found or disabled in groups database")

    except Exception as e:
        logger.warning(f"Failed to query database for groups enrichment: {e}")
        # Don't fail token validation if database is unavailable

    # Return original empty groups if no enrichment possible
    return current_groups


def should_enrich_groups(validation_result: dict) -> bool:
    """Check if groups should be enriched from MongoDB.

    Args:
        validation_result: Token validation result dictionary

    Returns:
        True if groups enrichment should be attempted
    """
    # Only enrich if:
    # 1. Token is valid
    # 2. Groups list is empty (not present or empty array)
    # 3. Has a client_id
    is_valid = validation_result.get("valid", False)
    groups = validation_result.get("groups", [])
    client_id = validation_result.get("client_id")

    return is_valid and not groups and client_id is not None


async def enrich_user_groups_from_mongodb(
    username: str,
    current_groups: list[str],
    provider: str,
) -> list[str]:
    """Enrich user groups from DocumentDB/MongoDB if current groups are empty.

    Mirrors enrich_groups_from_mongodb but reads the idp_user_groups
    collection and looks up by username. Used as a fallback when an IdP's
    user JWT does not carry a groups claim (e.g., PingFederate without the
    custom ATM groups attribute).

    Works with both AWS DocumentDB and MongoDB Community Edition.

    Args:
        username: Username (sub or preferred_username) from the JWT token
        current_groups: Current groups from JWT token
        provider: IdP name the token came from (e.g., "pingfederate"), used
            for logging and future per-provider scoping; the lookup itself
            is by username only.

    Returns:
        Enriched groups list (either from MongoDB or original)
    """
    # If groups already exist in token (non-empty array), use them
    if current_groups:
        # Count only: group names are organizational PII.
        logger.debug(f"User {username} has {len(current_groups)} groups in token")
        return current_groups

    logger.info(f"User {username} (provider={provider}) has no groups in token, querying database")

    # Try to fetch groups from DocumentDB/MongoDB
    try:
        db = await _get_mongodb()
        collection = db["idp_user_groups"]

        # Exclude operator-disabled records at the query level so a revoked
        # user-group mapping cannot keep granting access via the fallback.
        doc = await collection.find_one({"username": username, **_ENABLED_FILTER})

        if doc and not _is_record_enabled(doc):
            # Defense in depth: never trust groups from a disabled record even
            # if the query filter is ever loosened.
            logger.info(f"User {username} record is disabled; skipping enrichment")
            doc = None

        if doc:
            db_groups = doc.get("groups", [])
            if db_groups:
                logger.info(f"Enriched {len(db_groups)} groups for user {username} from database")
                return db_groups
            else:
                logger.debug(f"User {username} found in database but has no groups")
        else:
            logger.debug(f"User {username} not found or disabled in idp_user_groups database")

    except Exception as e:
        logger.warning(f"Failed to query database for user groups enrichment: {e}")
        # Don't fail token validation if database is unavailable

    # Return original empty groups if no enrichment possible
    return current_groups


def should_enrich_user_groups(
    username: str,
    current_groups: list[str],
    provider: str | None,
    enabled_providers: list[str],
) -> bool:
    """Check if user groups should be enriched from MongoDB.

    Args:
        username: Username from validated token
        current_groups: Current groups from validated token
        provider: IdP that issued the token (e.g., "pingfederate"); may be None
        enabled_providers: Lowercase list of providers for which user-group
            fallback is enabled. Compared case-insensitively.

    Returns:
        True iff provider is enabled, current groups are empty, and username
        is non-empty.
    """
    if not provider:
        return False

    if provider.lower() not in enabled_providers:
        return False

    if current_groups:
        return False

    if not username:
        return False

    return True
