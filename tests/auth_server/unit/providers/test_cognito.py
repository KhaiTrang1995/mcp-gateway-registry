"""
Unit tests for auth_server/providers/cognito.py

Currently scoped to the RFC 8414 metadata exposure added in issue #989. The
broader Cognito provider has historically been exercised via integration
tests; we add unit coverage here as new surface lands.
"""

import pytest


pytestmark = [pytest.mark.unit, pytest.mark.auth]


class TestCognitoAuthorizationServerMetadata:
    """Tests for RFC 8414 metadata exposure via authorization_server_metadata()."""

    def test_endpoints_rehomed_onto_cognito_domain(self):
        """authorization/token/userinfo/logout live on the cognito-domain host;
        only jwks_uri and issuer stay on cognito-idp.{region}.amazonaws.com."""
        from auth_server.providers.cognito import CognitoProvider

        provider = CognitoProvider(
            user_pool_id="us-east-1_abc123",
            client_id="c",
            client_secret="s",
            region="us-east-1",
            domain="my-app",
        )

        metadata = provider.authorization_server_metadata()

        assert metadata["issuer"] == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123"
        assert metadata["authorization_endpoint"].startswith(
            "https://my-app.auth.us-east-1.amazoncognito.com/"
        )
        assert metadata["token_endpoint"].startswith(
            "https://my-app.auth.us-east-1.amazoncognito.com/"
        )
        assert metadata["userinfo_endpoint"].startswith(
            "https://my-app.auth.us-east-1.amazoncognito.com/"
        )
        assert metadata["end_session_endpoint"].startswith(
            "https://my-app.auth.us-east-1.amazoncognito.com/"
        )
        # JWKS stays on the cognito-idp host
        assert (
            metadata["jwks_uri"]
            == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123/.well-known/jwks.json"
        )

    def test_default_domain_when_not_provided(self):
        """When no `domain` is configured, the cognito-domain host is derived
        from the user pool ID (Cognito's auto-generated domain)."""
        from auth_server.providers.cognito import CognitoProvider

        provider = CognitoProvider(
            user_pool_id="us-west-2_xyz789",
            client_id="c",
            client_secret="s",
            region="us-west-2",
        )

        metadata = provider.authorization_server_metadata()

        # Auto-derived domain strips the underscore from the user pool id
        assert "us-west-2xyz789.auth.us-west-2.amazoncognito.com" in metadata["token_endpoint"]
        assert metadata["issuer"] == "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_xyz789"

    def test_authorization_server_issuer_returns_cognito_idp_url(self):
        from auth_server.providers.cognito import CognitoProvider

        provider = CognitoProvider(
            user_pool_id="us-east-1_abc123",
            client_id="c",
            client_secret="s",
            region="us-east-1",
        )

        assert (
            provider.authorization_server_issuer()
            == "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc123"
        )

    def test_includes_pkce_support(self):
        """PKCE is mandatory in OAuth 2.1; metadata must advertise S256."""
        from auth_server.providers.cognito import CognitoProvider

        provider = CognitoProvider(
            user_pool_id="us-east-1_abc123",
            client_id="c",
            client_secret="s",
            region="us-east-1",
        )

        metadata = provider.authorization_server_metadata()

        assert "S256" in metadata["code_challenge_methods_supported"]
