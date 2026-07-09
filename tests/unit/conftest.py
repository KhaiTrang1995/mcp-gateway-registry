"""
Conftest for unit tests.

Provides fixtures specific to unit tests.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest

logger = logging.getLogger(__name__)


@pytest.fixture(autouse=True)
def _accept_internal_token_replay_check(request):
    """Bypass the single-use consumed-jti store in registry unit tests.

    ``validate_internal_auth`` records each internal token's ``jti`` in the
    shared MongoDB/DocumentDB store and fails closed (401) if it cannot reach
    it. Unit tests do not run against a live datastore, so without this the gate
    would reject every otherwise-valid internal token used by
    ``/api/internal/*`` route tests.

    The dedicated replay suite (``test_internal_token_replay``) needs the real
    ``consume_jti`` (it mocks the collection to drive the accept/replay-reject
    branches), so this fixture is a no-op there.
    """
    if request.module.__name__.endswith("test_internal_token_replay"):
        yield
        return
    with patch("registry.auth.internal.consume_jti", new=AsyncMock(return_value=True)):
        yield


@pytest.fixture
def mock_embeddings_client():
    """
    Create a mock embeddings client for testing.

    Returns:
        Mock embeddings client
    """
    from tests.fixtures.mocks.mock_embeddings import MockEmbeddingsClient

    return MockEmbeddingsClient()


@pytest.fixture
def mock_http_client():
    """
    Create a mock HTTP client for testing.

    Returns:
        Mock HTTP client
    """
    from tests.fixtures.mocks.mock_http import MockAsyncClient

    return MockAsyncClient()


@pytest.fixture
def mock_mcp_client():
    """
    Create a mock MCP client for testing.

    Returns:
        Mock MCP client with common methods
    """
    client = AsyncMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_tools = AsyncMock(return_value=[])
    client.call_tool = AsyncMock(return_value={})
    return client
