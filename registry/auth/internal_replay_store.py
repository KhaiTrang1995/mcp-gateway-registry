"""Single-use enforcement for internal service-to-service JWTs.

Internal tokens (see :mod:`registry.auth.internal`) are short-lived HS256 JWTs
minted immediately before a single service-to-service HTTP call. Their short TTL
limits — but does not eliminate — a replay window: a network-adjacent attacker on
the internal cluster network who captures a token can re-present it any number of
times until it expires.

This module closes that window by making each internal token **single-use**. Every
minted token carries a unique ``jti`` claim. The first time a token is validated,
its ``jti`` is atomically recorded in a shared MongoDB/DocumentDB collection with a
TTL equal to the token lifetime; a second validation of the same ``jti`` is
rejected as a replay.

Why the shared datastore (not a process-local set):
    The registry and auth-server run as separate processes (and each may run as
    multiple replicas). A captured token could be replayed against a *different*
    replica than the one that first consumed it. A process-local set would not see
    that. The existing MongoDB/DocumentDB backing store is shared across every
    replica of both services, so a unique index on ``jti`` there gives a truly
    cluster-wide single-use guarantee with no new infrastructure dependency.

Fail-closed:
    ``consume_jti`` returns ``False`` (reject the token) when the ``jti`` was
    already seen AND when the store is unreachable or errors. A replay-protection
    layer that silently allows on error is equivalent to no protection, so any
    ambiguity denies. The TTL index reaps consumed entries automatically, so the
    collection stays bounded even under sustained load.
"""

import logging
from datetime import UTC, datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

# Base collection name; namespaced per-tenant via ``get_collection_name``.
_COLLECTION_BASE_NAME: str = "internal_token_jti"

# Safety margin added to the token TTL when computing the consumed-jti document's
# own expiry, so a document is never reaped by the TTL index while the token it
# guards could still be presented (token TTL + clock leeway). Generous on purpose:
# an over-retained jti only costs a little storage; an under-retained one reopens
# the replay window.
_JTI_RETENTION_MARGIN_SECONDS: int = 120

_collection: AsyncIOMotorCollection | None = None
_indexes_created: bool = False


async def _get_collection() -> AsyncIOMotorCollection:
    """Return the consumed-jti collection, creating indexes on first access.

    Reuses the registry's shared DocumentDB/MongoDB client so both the registry
    and auth-server processes (and all their replicas) target the same collection.

    Returns:
        The Motor collection used to record consumed ``jti`` values.

    Raises:
        Exception: Propagates any connection/setup failure so the caller can
            fail closed (deny the token) rather than skip the replay check.
    """
    global _collection, _indexes_created

    if _collection is not None:
        return _collection

    from registry.repositories.documentdb.client import get_collection_name, get_documentdb_client

    db = await get_documentdb_client()
    collection_name = get_collection_name(_COLLECTION_BASE_NAME)
    collection = db[collection_name]

    if not _indexes_created:
        # Unique index on ``jti`` is what makes the insert atomic: a concurrent
        # or subsequent insert of a seen jti raises DuplicateKeyError, which we
        # treat as a replay. TTL index reaps documents after the token can no
        # longer be valid, bounding the collection size.
        await collection.create_index([("jti", ASCENDING)], unique=True, name="ux_jti")
        await collection.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            name="ttl_expires_at",
        )
        _indexes_created = True
        logger.info(f"Created indexes for {collection_name} collection")

    _collection = collection
    return _collection


async def consume_jti(
    jti: str | None,
    ttl_seconds: int,
) -> bool:
    """Atomically record a token's ``jti`` as consumed; reject replays.

    The first call for a given ``jti`` inserts a document and returns ``True``
    (accept). Any subsequent call with the same ``jti`` hits the unique index,
    raises :class:`~pymongo.errors.DuplicateKeyError`, and returns ``False``
    (reject — this is a replay). A store error also returns ``False`` so the
    caller fails closed.

    Args:
        jti: The token's unique identifier claim, or ``None``/empty when the
            token carries no ``jti`` (which is rejected — fail closed).
        ttl_seconds: The token's TTL in seconds; used to compute how long the
            consumed-jti record is retained before the TTL index reaps it.

    Returns:
        ``True`` if this is the first time ``jti`` was seen (accept the token),
        ``False`` if it was already consumed or the store could not be reached
        (reject the token).
    """
    if not jti:
        # No jti means no replay protection is possible — deny.
        logger.warning("Internal token rejected: missing jti claim")
        return False

    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds + _JTI_RETENTION_MARGIN_SECONDS)

    try:
        collection = await _get_collection()
        await collection.insert_one(
            {
                "jti": jti,
                "consumed_at": now,
                "expires_at": expires_at,
            }
        )
        return True
    except DuplicateKeyError:
        logger.warning("Internal token replay rejected: jti already consumed")
        return False
    except Exception as exc:
        # Fail closed: if we cannot record the jti, we cannot guarantee
        # single-use, so we must deny rather than risk an undetected replay.
        logger.error(f"Internal token replay check failed (denying): {exc}")
        return False


def _reset_state_for_tests() -> None:
    """Reset the module-level singletons. Test-only helper."""
    global _collection, _indexes_created
    _collection = None
    _indexes_created = False
