"""Unit tests for single-use enforcement on internal service tokens.

Internal service JWTs (``registry/auth/internal.py``) are short-lived HS256
tokens minted right before a single service-to-service call. Before this change
they carried only ``iat``/``exp``, so a network-adjacent attacker who captured
one could replay it any number of times within the TTL window on the internal
cluster network.

The fix adds a unique ``jti`` claim and a shared consumed-jti store: the first
validation records the ``jti``; a replay of the same token is rejected. These
tests pin:

1. A minted token carries a unique ``jti``.
2. A token with a fresh ``jti`` is accepted exactly once.
3. The SAME token replayed is rejected (jti already consumed).
4. A token with no ``jti`` is rejected (fail closed).
5. A store failure rejects the token (fail closed).
6. Signature/expiry validation still runs before the single-use check.
"""

import os
import time
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError
from starlette.requests import Request

from registry.auth import internal_replay_store
from registry.auth.internal import (
    _INTERNAL_JWT_AUDIENCE,
    _INTERNAL_JWT_ISSUER,
    _INTERNAL_TOKEN_KIND,
    _derive_internal_signing_key,
    generate_internal_token,
    validate_internal_auth,
)
from registry.auth.internal_replay_store import consume_jti

_SECRET_KEY: str = "x" * 40  # >= 32 bytes so the config-level guard is satisfied


def _make_request(token: str | None) -> Request:
    """Build a minimal ASGI Request carrying an optional Bearer token."""
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {"type": "http", "method": "POST", "path": "/internal/x", "headers": headers}
    return Request(scope)


class _FakeConsumedStore:
    """In-memory stand-in for the shared consumed-jti collection.

    Mimics the atomic unique-index behaviour: the first insert of a jti
    succeeds; a second raises DuplicateKeyError (what a real unique index does).
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def insert_one(self, doc: dict) -> None:
        jti = doc["jti"]
        if jti in self._seen:
            raise DuplicateKeyError("duplicate jti")
        self._seen.add(jti)


class TestGeneratedTokenCarriesJti:
    """A minted internal token must carry a unique jti claim."""

    def test_token_has_jti(self) -> None:
        with patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}):
            token = generate_internal_token(subject="registry-service", purpose="test")
            claims = pyjwt.decode(
                token,
                _derive_internal_signing_key(_SECRET_KEY),
                algorithms=["HS256"],
                audience=_INTERNAL_JWT_AUDIENCE,
                issuer=_INTERNAL_JWT_ISSUER,
            )
        assert claims.get("jti")
        assert claims["token_kind"] == _INTERNAL_TOKEN_KIND

    def test_two_tokens_have_distinct_jti(self) -> None:
        with patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}):
            t1 = generate_internal_token(subject="s", purpose="p")
            t2 = generate_internal_token(subject="s", purpose="p")
            key = _derive_internal_signing_key(_SECRET_KEY)
            j1 = pyjwt.decode(
                t1,
                key,
                algorithms=["HS256"],
                audience=_INTERNAL_JWT_AUDIENCE,
                issuer=_INTERNAL_JWT_ISSUER,
            )["jti"]
            j2 = pyjwt.decode(
                t2,
                key,
                algorithms=["HS256"],
                audience=_INTERNAL_JWT_AUDIENCE,
                issuer=_INTERNAL_JWT_ISSUER,
            )["jti"]
        assert j1 != j2


class TestConsumeJti:
    """Direct tests of the shared consumed-jti store."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_first_use_accepted_replay_rejected(self) -> None:
        fake = _FakeConsumedStore()
        with patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)):
            assert await consume_jti("abc123", 60) is True
            # Same jti a second time -> DuplicateKeyError -> reject.
            assert await consume_jti("abc123", 60) is False
            # A different jti is still accepted.
            assert await consume_jti("def456", 60) is True

    @pytest.mark.asyncio
    async def test_empty_jti_rejected(self) -> None:
        # No store access should even be attempted for an empty jti.
        assert await consume_jti("", 60) is False

    @pytest.mark.asyncio
    async def test_store_unreachable_rejected(self) -> None:
        with patch.object(
            internal_replay_store,
            "_get_collection",
            AsyncMock(side_effect=RuntimeError("mongo down")),
        ):
            assert await consume_jti("abc123", 60) is False


class TestValidateInternalAuthSingleUse:
    """End-to-end single-use enforcement through validate_internal_auth."""

    def teardown_method(self) -> None:
        internal_replay_store._reset_state_for_tests()

    @pytest.mark.asyncio
    async def test_fresh_token_accepted_once_then_replay_rejected(self) -> None:
        fake = _FakeConsumedStore()
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)),
        ):
            token = generate_internal_token(subject="registry-service", purpose="test")

            # First presentation: accepted, returns caller identity.
            caller = await validate_internal_auth(_make_request(token))
            assert caller == "registry-service"

            # Replay of the exact same token: rejected 401.
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_token_without_jti_rejected(self) -> None:
        """A validly-signed internal token that omits jti is rejected (fail closed)."""
        fake = _FakeConsumedStore()
        now = int(time.time())
        claims = {
            "iss": _INTERNAL_JWT_ISSUER,
            "aud": _INTERNAL_JWT_AUDIENCE,
            "sub": "registry-service",
            "token_kind": _INTERNAL_TOKEN_KIND,
            "token_use": "access",
            "iat": now,
            "exp": now + 60,
            # no jti
        }
        token = pyjwt.encode(claims, _derive_internal_signing_key(_SECRET_KEY), algorithm="HS256")
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", AsyncMock(return_value=fake)),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid token"

    @pytest.mark.asyncio
    async def test_expired_token_rejected_before_jti_check(self) -> None:
        """Expiry still enforced; store is never consulted for an expired token."""
        now = int(time.time())
        claims = {
            "iss": _INTERNAL_JWT_ISSUER,
            "aud": _INTERNAL_JWT_AUDIENCE,
            "sub": "registry-service",
            "token_kind": _INTERNAL_TOKEN_KIND,
            "token_use": "access",
            "jti": "expired-jti",
            "iat": now - 300,
            "exp": now - 120,
        }
        token = pyjwt.encode(claims, _derive_internal_signing_key(_SECRET_KEY), algorithm="HS256")
        get_collection = AsyncMock()
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(internal_replay_store, "_get_collection", get_collection),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
        assert exc_info.value.status_code == 401
        get_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_failure_rejects_valid_token(self) -> None:
        """A signature-valid, fresh token is denied when the store is unreachable."""
        with (
            patch.dict(os.environ, {"SECRET_KEY": _SECRET_KEY}),
            patch.object(
                internal_replay_store,
                "_get_collection",
                AsyncMock(side_effect=RuntimeError("mongo down")),
            ),
        ):
            token = generate_internal_token(subject="registry-service", purpose="test")
            with pytest.raises(HTTPException) as exc_info:
                await validate_internal_auth(_make_request(token))
        assert exc_info.value.status_code == 401
