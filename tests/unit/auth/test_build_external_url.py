"""Tests for _build_external_url helper in auth routes.

Verifies that OAuth2 redirect URIs include ROOT_PATH when path-based
routing is enabled (issue #500), and that the inbound Host header is
validated against a trusted allowlist before it feeds the redirect URI
(Host-header trust hardening).
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from registry.auth import routes
from registry.auth.routes import _build_external_url
from registry.core.config import Settings


@pytest.fixture(autouse=True)
def _trust_test_hosts():
    """Trust the hostnames used by the behavioral tests below.

    The security allowlist derives from registry_url by default; these
    behavioral tests use example.com / custom ports, so we widen the allowlist
    for them. The dedicated Host-allowlist tests below override this to assert
    the fail-closed rejection path.
    """
    trusted = {
        "example.com",
        "localhost",
        "localhost:3000",
        "localhost:7860",
    }
    with patch.object(
        Settings,
        "trusted_external_hosts_set",
        new_callable=PropertyMock,
        return_value=trusted,
    ):
        yield


def _make_request(
    host: str = "example.com",
    scheme: str = "https",
    cloudfront_proto: str = "",
    x_forwarded_proto: str = "",
) -> MagicMock:
    """Create a mock Request with configurable headers and scheme."""
    request = MagicMock()
    header_dict = {
        "host": host,
        "x-cloudfront-forwarded-proto": cloudfront_proto,
        "x-forwarded-proto": x_forwarded_proto,
    }
    request.headers = MagicMock()
    request.headers.get = lambda key, default="": header_dict.get(key, default)
    request.url = MagicMock()
    request.url.scheme = scheme
    return request


class TestBuildExternalUrlWithoutRootPath:
    """Tests when ROOT_PATH is empty (subdomain mode, Docker, ECS)."""

    def setup_method(self):
        self._original = routes._ROOT_PATH
        routes._ROOT_PATH = ""

    def teardown_method(self):
        routes._ROOT_PATH = self._original

    def test_basic_url(self):
        request = _make_request()
        result = _build_external_url(request, "/logout")
        assert result == "https://example.com/logout"

    def test_root_path_url(self):
        request = _make_request()
        result = _build_external_url(request, "/")
        assert result == "https://example.com/"

    def test_empty_path(self):
        request = _make_request()
        result = _build_external_url(request)
        assert result == "https://example.com"

    def test_http_scheme(self):
        request = _make_request(scheme="http")
        result = _build_external_url(request, "/logout")
        assert result == "http://example.com/logout"


class TestBuildExternalUrlWithRootPath:
    """Tests when ROOT_PATH is set (path-based routing, EKS)."""

    def setup_method(self):
        self._original = routes._ROOT_PATH
        routes._ROOT_PATH = "/registry"

    def teardown_method(self):
        routes._ROOT_PATH = self._original

    def test_logout_includes_root_path(self):
        request = _make_request()
        result = _build_external_url(request, "/logout")
        assert result == "https://example.com/registry/logout"

    def test_root_includes_root_path(self):
        request = _make_request()
        result = _build_external_url(request, "/")
        assert result == "https://example.com/registry/"

    def test_empty_path_with_root(self):
        request = _make_request()
        result = _build_external_url(request)
        assert result == "https://example.com/registry"

    def test_deep_root_path(self):
        routes._ROOT_PATH = "/app/registry"
        request = _make_request()
        result = _build_external_url(request, "/logout")
        assert result == "https://example.com/app/registry/logout"


class TestBuildExternalUrlSchemeDetection:
    """Tests for HTTPS detection from proxy headers."""

    def setup_method(self):
        self._original = routes._ROOT_PATH
        routes._ROOT_PATH = ""

    def teardown_method(self):
        routes._ROOT_PATH = self._original

    def test_cloudfront_header_forces_https(self):
        request = _make_request(scheme="http", cloudfront_proto="https")
        result = _build_external_url(request, "/logout")
        assert result.startswith("https://")

    def test_x_forwarded_proto_forces_https(self):
        request = _make_request(scheme="http", x_forwarded_proto="https")
        result = _build_external_url(request, "/logout")
        assert result.startswith("https://")

    def test_request_scheme_https(self):
        request = _make_request(scheme="https")
        result = _build_external_url(request, "/logout")
        assert result.startswith("https://")

    def test_all_http_stays_http(self):
        request = _make_request(scheme="http")
        result = _build_external_url(request, "/logout")
        assert result.startswith("http://")


class TestBuildExternalUrlLocalhostHandling:
    """Tests for localhost special case (adds port if missing)."""

    def setup_method(self):
        self._original = routes._ROOT_PATH
        routes._ROOT_PATH = ""

    def teardown_method(self):
        routes._ROOT_PATH = self._original

    def test_localhost_without_port_gets_default(self):
        request = _make_request(host="localhost", scheme="http")
        result = _build_external_url(request, "/logout")
        assert result == "http://localhost:7860/logout"

    def test_localhost_with_port_preserved(self):
        request = _make_request(host="localhost:3000", scheme="http")
        result = _build_external_url(request, "/logout")
        assert result == "http://localhost:3000/logout"

    def test_localhost_with_root_path(self):
        routes._ROOT_PATH = "/registry"
        request = _make_request(host="localhost", scheme="http")
        result = _build_external_url(request, "/logout")
        assert result == "http://localhost:7860/registry/logout"


class TestBuildExternalUrlPathNormalization:
    """Tests that path argument is normalized correctly."""

    def setup_method(self):
        self._original = routes._ROOT_PATH
        routes._ROOT_PATH = ""

    def teardown_method(self):
        routes._ROOT_PATH = self._original

    def test_path_without_leading_slash_gets_one(self):
        request = _make_request()
        result = _build_external_url(request, "logout")
        assert result == "https://example.com/logout"

    def test_path_with_leading_slash_preserved(self):
        request = _make_request()
        result = _build_external_url(request, "/logout")
        assert result == "https://example.com/logout"


class TestBuildExternalUrlHostAllowlist:
    """Host-header trust: an untrusted Host must not feed the redirect URI."""

    def setup_method(self):
        self._original = routes._ROOT_PATH
        routes._ROOT_PATH = ""

    def teardown_method(self):
        routes._ROOT_PATH = self._original

    def test_trusted_host_is_used(self):
        with patch.object(
            Settings,
            "trusted_external_hosts_set",
            new_callable=PropertyMock,
            return_value={"app.example.com"},
        ):
            request = _make_request(host="app.example.com")
            result = _build_external_url(request, "/logout")
            assert result == "https://app.example.com/logout"

    def test_untrusted_host_falls_back_to_registry_host(self):
        """A spoofed Host is rejected; the URL uses the configured registry host."""
        with (
            patch.object(
                Settings,
                "trusted_external_hosts_set",
                new_callable=PropertyMock,
                return_value={"app.example.com"},
            ),
            patch.object(routes.settings, "registry_url", "https://real.example.com"),
        ):
            request = _make_request(host="evil.attacker.example")
            result = _build_external_url(request, "/logout")
            assert "evil.attacker.example" not in result
            assert result == "https://real.example.com/logout"

    def test_missing_host_falls_back(self):
        with (
            patch.object(
                Settings,
                "trusted_external_hosts_set",
                new_callable=PropertyMock,
                return_value={"app.example.com"},
            ),
            patch.object(routes.settings, "registry_url", "https://real.example.com"),
        ):
            request = _make_request(host="")
            result = _build_external_url(request, "/logout")
            assert result == "https://real.example.com/logout"

    def test_host_match_is_case_insensitive(self):
        with patch.object(
            Settings,
            "trusted_external_hosts_set",
            new_callable=PropertyMock,
            return_value={"app.example.com"},
        ):
            request = _make_request(host="APP.Example.COM")
            result = _build_external_url(request, "/logout")
            # Host is echoed as-supplied but only because it matched the
            # allowlist case-insensitively.
            assert result == "https://APP.Example.COM/logout"
