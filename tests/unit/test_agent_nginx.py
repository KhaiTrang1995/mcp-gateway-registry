"""Unit tests for A2A agent nginx reverse-proxy config generation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from registry.core.nginx_service import NginxConfigService


def _agent(
    path="/flight-booking-agent",
    url="https://flight-booking.dev.example.com",
    name="Flight Booking Agent",
    supported_protocol="a2a",
    health_status="healthy",
):
    """Build a lightweight agent-card stand-in for the generator.

    The generator reads ``path``, ``url``, ``name``, ``supported_protocol``
    and ``health_status``, so a namespace avoids coupling the test to the full
    AgentCard pydantic model. Defaults describe a healthy A2A agent (the case
    that produces a proxy block).
    """
    return SimpleNamespace(
        path=path,
        url=url,
        name=name,
        supported_protocol=supported_protocol,
        health_status=health_status,
    )


@pytest.fixture
def patched_agent_service():
    """Patch the agent_service singleton imported lazily by the generator."""
    with patch("registry.services.agent_service.agent_service") as mock_svc:
        mock_svc.get_enabled_agents = AsyncMock(return_value=[])
        mock_svc.get_agent_info = AsyncMock(return_value=None)
        yield mock_svc


class TestGenerateAgentLocationBlocks:
    """Tests for _generate_agent_location_blocks."""

    @pytest.mark.asyncio
    async def test_no_enabled_agents_returns_empty(self, patched_agent_service):
        """Empty string returned when there are no enabled agents."""
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_generates_blocks_for_enabled_agent(self, patched_agent_service):
        """An enabled agent produces a location block at /agent/{path}/."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=_agent())
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert "{{ROOT_PATH}}/agent/flight-booking-agent/" in result

    @pytest.mark.asyncio
    async def test_includes_agent_card_discovery_location(self, patched_agent_service):
        """The agent-card discovery location is emitted."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=_agent())
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert "{{ROOT_PATH}}/agent/flight-booking-agent/.well-known/agent-card.json" in result

    @pytest.mark.asyncio
    async def test_block_enforces_auth_request(self, patched_agent_service):
        """Generated blocks are protected by the /validate auth subrequest."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=_agent())
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert "auth_request /validate;" in result

    @pytest.mark.asyncio
    async def test_jsonrpc_block_disables_buffering_for_sse(self, patched_agent_service):
        """The JSON-RPC block disables proxy buffering for message/stream SSE."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=_agent())
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert "proxy_buffering off;" in result

    @pytest.mark.asyncio
    async def test_proxies_to_backend_url(self, patched_agent_service):
        """The block proxies to the agent backend url."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=_agent())
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert "proxy_pass https://flight-booking.dev.example.com/;" in result

    @pytest.mark.asyncio
    async def test_skips_agent_without_url(self, patched_agent_service):
        """An enabled agent with no backend url is skipped."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=_agent(url=""))
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_missing_agent_card(self, patched_agent_service):
        """An enabled path with no resolvable card is skipped."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(return_value=None)
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_agent_with_unsafe_path(self, patched_agent_service):
        """An agent whose path would inject nginx directives is skipped."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/evil"])
        patched_agent_service.get_agent_info = AsyncMock(
            return_value=_agent(path="evil}\n location / { return 200; }\n #")
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_agent_with_unsafe_url(self, patched_agent_service):
        """An agent whose backend url is not a safe http(s) url is skipped."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/evil"])
        patched_agent_service.get_agent_info = AsyncMock(
            return_value=_agent(url="https://host/ { return 200; }")
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_unhealthy_agent(self, patched_agent_service):
        """An unhealthy agent is skipped so no route points at a dead backend."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(
            return_value=_agent(health_status="unhealthy")
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_agent_with_unknown_health(self, patched_agent_service):
        """An agent whose health is not yet verified (unknown) is skipped."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(
            return_value=_agent(health_status="unknown")
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_non_a2a_protocol_agent(self, patched_agent_service):
        """A non-A2A agent with a URL does not get a JSON-RPC proxy block."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(
            return_value=_agent(supported_protocol="other")
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_skips_agent_missing_protocol(self, patched_agent_service):
        """An agent with no supported_protocol is skipped (not treated as A2A)."""
        patched_agent_service.get_enabled_agents = AsyncMock(return_value=["/flight-booking-agent"])
        patched_agent_service.get_agent_info = AsyncMock(
            return_value=_agent(supported_protocol=None)
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""

    @pytest.mark.asyncio
    async def test_generates_blocks_for_multiple_agents(self, patched_agent_service):
        """Each enabled agent gets its own route."""
        patched_agent_service.get_enabled_agents = AsyncMock(
            return_value=["/flight-booking-agent", "/travel-assistant-agent"]
        )

        async def _info(path):
            return _agent(path=path, url=f"https://flight-booking{path}.example.com")

        patched_agent_service.get_agent_info = AsyncMock(side_effect=_info)
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert "{{ROOT_PATH}}/agent/flight-booking-agent/" in result
        assert "{{ROOT_PATH}}/agent/travel-assistant-agent/" in result

    @pytest.mark.asyncio
    async def test_generation_failure_returns_empty(self, patched_agent_service):
        """A failure while resolving agents must not break config rendering.

        The generator fails closed to an empty string so nginx still reloads
        (without any agent proxy blocks) instead of raising.
        """
        patched_agent_service.get_enabled_agents = AsyncMock(
            side_effect=RuntimeError("agent store unavailable")
        )
        service = NginxConfigService()

        result = await service._generate_agent_location_blocks()

        assert result == ""


class TestReverseProxyFlagDefault:
    """The reverse-proxy mode must be opt-in."""

    def test_flag_defaults_to_disabled(self):
        """A2A_REVERSE_PROXY_ENABLED defaults to False (opt-in)."""
        from registry.core.config import Settings

        assert Settings.model_fields["a2a_reverse_proxy_enabled"].default is False


class TestCreateAgentLocationBlock:
    """Tests for _create_agent_location_block."""

    def test_external_host_uses_upstream_hostname(self):
        """An https backend sets the Host header to the upstream hostname."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert "proxy_set_header Host flight-booking.dev.example.com;" in block

    def test_internal_host_preserves_original_host(self):
        """A bare internal hostname preserves the original Host header."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "http://flight-booking-agent",
            "Flight Booking Agent",
        )

        assert "proxy_set_header Host $host;" in block

    def test_captures_body_for_metrics(self):
        """The JSON-RPC block captures the request body so metrics can record the method."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert "rewrite_by_lua_file /etc/nginx/lua/capture_body.lua;" in block

    def test_card_location_is_exact_match(self):
        """The agent-card location uses an exact match to block suffix smuggling."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert (
            "location = {{ROOT_PATH}}/agent/flight-booking-agent/.well-known/agent-card.json"
            in block
        )

    def test_card_proxy_pass_has_no_double_slash(self):
        """The agent-card proxy_pass targets the backend without a double slash."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert (
            "proxy_pass https://flight-booking.dev.example.com/.well-known/agent-card.json;"
            in block
        )

    def test_streaming_read_timeout_present(self):
        """The JSON-RPC block sets a long read timeout for SSE streaming."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert "proxy_read_timeout 86400s;" in block

    def test_dotted_internal_hostname_uses_upstream_netloc(self):
        """A dotted internal hostname (with port) sets Host to the upstream netloc."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "http://svc.namespace.svc.cluster.local:8080",
            "Flight Booking Agent",
        )

        assert "proxy_set_header Host svc.namespace.svc.cluster.local:8080;" in block

    def test_unsafe_path_raises(self):
        """An agent path with nginx metacharacters raises ValueError."""
        service = NginxConfigService()

        with pytest.raises(ValueError, match="unsafe agent path"):
            service._create_agent_location_block(
                "evil}\n location / {}",
                "https://flight-booking.dev.example.com",
                "Evil",
            )

    def test_unsafe_url_raises(self):
        """A backend url with nginx metacharacters raises ValueError."""
        service = NginxConfigService()

        with pytest.raises(ValueError, match="unsafe agent url"):
            service._create_agent_location_block(
                "flight-booking-agent",
                "https://host/ { return 200; }",
                "Evil",
            )

    def test_card_location_rewrites_card_urls(self):
        """The agent-card location runs the card-rewrite body filter."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert "body_filter_by_lua_file /etc/nginx/lua/agent_card_rewrite.lua;" in block

    def test_card_location_clears_content_length(self):
        """Content-Length is cleared so the rewritten card body is not truncated."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert "ngx.header.content_length = nil" in block

    def test_jsonrpc_block_forwards_scopes_to_backend(self):
        """Validated scopes are captured from /validate and forwarded to the agent."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "flight-booking-agent",
            "https://flight-booking.dev.example.com",
            "Flight Booking Agent",
        )

        assert "auth_request_set $auth_scopes $upstream_http_x_scopes;" in block
        assert "proxy_set_header X-Scopes $auth_scopes;" in block

    def test_multi_segment_agent_path_in_route(self):
        """A multi-segment agent path renders verbatim in the location route."""
        service = NginxConfigService()

        block = service._create_agent_location_block(
            "lob1/travel",
            "https://flight-booking.dev.example.com",
            "Travel",
        )

        assert "{{ROOT_PATH}}/agent/lob1/travel/" in block
