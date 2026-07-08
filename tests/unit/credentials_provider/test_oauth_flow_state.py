"""Tests for OAuth CSRF state validation in the credential-provider flow.

These tests exercise the fail-closed state check in
``credentials-provider/oauth/generic_oauth_flow.py``. The core invariant: a
callback whose ``state`` does not match the state the flow generated must never
reach the token exchange. This defends the local OAuth callback handler against
CSRF / authorization-code injection where an attacker forges the callback with
their own ``code``.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The credential provider ships as standalone scripts, not an installed package,
# so put its oauth directory on the path before importing the module under test.
_OAUTH_DIR = Path(__file__).resolve().parents[3] / "credentials-provider" / "oauth"
sys.path.insert(0, str(_OAUTH_DIR))

import generic_oauth_flow as flow  # noqa: E402


class _RecordingHandler(flow.CallbackHandler):
    """CallbackHandler with the socket plumbing stubbed out.

    ``do_GET`` only reads ``self.path`` and calls ``self._send_response``, so we
    bypass ``BaseHTTPRequestHandler.__init__`` (which needs a real socket) and
    record what the handler would have sent back to the browser.
    """

    def __init__(self, path: str) -> None:  # noqa: D401 - test double
        self.path = path
        self.sent: list[tuple[str, int]] = []

    def _send_response(self, message: str, status: int = 200) -> None:
        self.sent.append((message, status))


@pytest.fixture(autouse=True)
def _reset_flow_globals():
    """Reset the module globals the handler reads/writes for each test."""
    flow.authorization_code = None
    flow.received_state = None
    flow.expected_state = None
    flow.callback_received = False
    flow.callback_error = None
    flow.oauth_config_global = None
    yield
    flow.oauth_config_global = None


def _make_config_that_records_exchange() -> MagicMock:
    config = MagicMock()
    config.exchange_code_for_tokens.return_value = True
    return config


class TestCallbackStateValidation:
    """The inline callback token exchange must be gated on the CSRF state."""

    def test_matching_state_allows_token_exchange(self):
        flow.expected_state = "the-real-state"
        config = _make_config_that_records_exchange()
        flow.oauth_config_global = config

        handler = _RecordingHandler("/callback?code=good-code&state=the-real-state")
        handler.do_GET()

        assert flow.callback_error is None
        assert flow.authorization_code == "good-code"
        config.exchange_code_for_tokens.assert_called_once()

    def test_mismatched_state_blocks_token_exchange(self):
        flow.expected_state = "the-real-state"
        config = _make_config_that_records_exchange()
        flow.oauth_config_global = config

        # Attacker-forged callback: valid-looking code, wrong state.
        handler = _RecordingHandler("/callback?code=attacker-code&state=attacker-state")
        handler.do_GET()

        # Fail closed: no exchange, error recorded, 400 to the browser.
        config.exchange_code_for_tokens.assert_not_called()
        assert flow.callback_error == "state_mismatch"
        assert handler.sent and handler.sent[-1][1] == 400

    def test_missing_state_in_callback_blocks_token_exchange(self):
        flow.expected_state = "the-real-state"
        config = _make_config_that_records_exchange()
        flow.oauth_config_global = config

        handler = _RecordingHandler("/callback?code=attacker-code")
        handler.do_GET()

        config.exchange_code_for_tokens.assert_not_called()
        assert flow.callback_error == "state_mismatch"

    def test_no_expected_state_blocks_token_exchange(self):
        # expected_state never set (e.g. spurious/replayed callback with no
        # in-flight authorization request) must also fail closed.
        flow.expected_state = None
        config = _make_config_that_records_exchange()
        flow.oauth_config_global = config

        handler = _RecordingHandler("/callback?code=code&state=anything")
        handler.do_GET()

        config.exchange_code_for_tokens.assert_not_called()
        assert flow.callback_error == "state_mismatch"
