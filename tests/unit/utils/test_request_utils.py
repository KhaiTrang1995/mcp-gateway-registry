"""
Unit tests for registry.utils.request_utils.

Validates NON-SPOOFABLE client-IP extraction from proxied requests. The
left-most X-Forwarded-For entry is attacker-controlled and must never be
trusted; the resolver prefers X-Real-IP (set by the trusted proxy) or the
trusted right-most XFF hop, and fails toward the direct socket peer.
"""

from unittest.mock import MagicMock

from registry.utils.request_utils import get_client_ip


def _make_request(headers=None, client_host="127.0.0.1", client=None):
    """Create a minimal mock FastAPI Request.

    Headers are wrapped in a case-insensitive mapping to mirror Starlette's
    Headers object (the real request uses case-insensitive lookups).
    """

    class _CIHeaders(dict):
        def get(self, key, default=None):
            lower = key.lower()
            for k, v in self.items():
                if k.lower() == lower:
                    return v
            return default

    request = MagicMock()
    request.headers = _CIHeaders(headers or {})
    if client is False:
        request.client = None
    else:
        request.client = MagicMock()
        request.client.host = client_host
    return request


class TestGetClientIpTrustedHop:
    """The resolver must ignore the spoofable left-most XFF entry."""

    def test_prefers_x_real_ip(self):
        """X-Real-IP (set by the trusted proxy) wins over any XFF value."""
        request = _make_request(
            headers={
                "X-Real-IP": "203.0.113.9",
                "X-Forwarded-For": "1.2.3.4, 203.0.113.9",
            },
        )
        assert get_client_ip(request) == "203.0.113.9"

    def test_ignores_spoofed_leftmost_xff(self, monkeypatch):
        """A forged left-most XFF entry is NOT returned; the trusted hop is.

        With one trusted proxy, nginx appends the real peer as the right-most
        entry via $proxy_add_x_forwarded_for. An attacker prepends a fake IP;
        we must return the appended (right-most) hop, not the fake.
        """
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.10"},
        )
        assert get_client_ip(request) == "203.0.113.10"

    def test_multiple_trusted_hops(self, monkeypatch):
        """With 2 trusted hops, the client IP is 2 from the right."""
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.20, 10.0.0.1"},
        )
        # parts[-2] == 203.0.113.20 (the outer trusted proxy's observed peer)
        assert get_client_ip(request) == "203.0.113.20"

    def test_falls_back_to_peer_when_no_headers(self):
        request = _make_request(client_host="10.0.0.5")
        assert get_client_ip(request) == "10.0.0.5"

    def test_returns_unknown_when_no_client(self):
        request = _make_request(client=False)
        assert get_client_ip(request) == "unknown"

    def test_rejects_malformed_real_ip_then_uses_xff_hop(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        request = _make_request(
            headers={
                "X-Real-IP": "<script>alert(1)</script>",
                "X-Forwarded-For": "6.6.6.6, 203.0.113.30",
            },
            client_host="10.0.0.1",
        )
        assert get_client_ip(request) == "203.0.113.30"

    def test_rejects_malformed_xff_hop_falls_back_to_peer(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6, not-an-ip"},
            client_host="10.0.0.1",
        )
        assert get_client_ip(request) == "10.0.0.1"

    def test_zero_trusted_hops_ignores_xff_entirely(self, monkeypatch):
        """TRUSTED_PROXY_HOPS=0 means no proxy is trusted: use the peer only."""
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "0")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.40"},
            client_host="10.0.0.9",
        )
        assert get_client_ip(request) == "10.0.0.9"

    def test_invalid_hops_env_defaults_to_one(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "garbage")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.50"},
        )
        assert get_client_ip(request) == "203.0.113.50"

    def test_handles_ipv6_hop(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6, 2001:db8::1"},
        )
        assert get_client_ip(request) == "2001:db8::1"

    def test_empty_forwarded_for_falls_back_to_peer(self):
        request = _make_request(
            headers={"X-Forwarded-For": ""},
            client_host="10.0.0.1",
        )
        assert get_client_ip(request) == "10.0.0.1"

    def test_shorter_chain_than_hops_falls_back_to_peer(self, monkeypatch):
        """If the chain is shorter than the trusted hop count, XFF is ignored
        entirely (every entry is client-controlled) and the direct peer is used.

        This is the fail-closed path: a forged single-entry XFF must NOT win
        when the configured proxy depth was not actually traversed.
        """
        monkeypatch.setenv("TRUSTED_PROXY_HOPS", "3")
        request = _make_request(
            headers={"X-Forwarded-For": "6.6.6.6"},
            client_host="10.0.0.1",
        )
        assert get_client_ip(request) == "10.0.0.1"
