"""Unit tests for the /validate rate-limit target classifier (auth_server.server)."""

import pytest


@pytest.mark.unit
class TestClassifyTarget:
    """Tests for _classify_rate_limit_target."""

    def test_mcp_server_target(self):
        """A plain MCP server path classifies as mcp_server."""
        from auth_server.server import _classify_rate_limit_target

        entity_type, name = _classify_rate_limit_target(
            "https://gw.example.com/mcpgw/mcp", "mcpgw"
        )
        assert entity_type == "mcp_server"
        assert name == "mcpgw"

    def test_a2a_agent_target(self):
        """An /agent/ path classifies as a2a_agent with the agent path."""
        from auth_server.server import _classify_rate_limit_target

        entity_type, name = _classify_rate_limit_target(
            "https://gw.example.com/agent/booking-agent/", "booking-agent"
        )
        assert entity_type == "a2a_agent"
        assert name == "/booking-agent"

    def test_no_target(self):
        """A request with neither an agent path nor a server name yields (None, None)."""
        from auth_server.server import _classify_rate_limit_target

        entity_type, name = _classify_rate_limit_target(None, None)
        assert entity_type is None
        assert name is None
