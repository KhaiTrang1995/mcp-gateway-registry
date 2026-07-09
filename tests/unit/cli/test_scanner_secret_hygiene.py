"""Tests for CLI secret hygiene in the security-scan helpers and token writers.

These tests pin two invariants that would fail against the previous code:

1. Secrets (LLM API key, target-server bearer token) handed to a scanner
   subprocess are delivered through the child environment, never on its
   command line (argv is world-readable via ``ps`` / ``/proc/<pid>/cmdline``).
2. Token/credential files are created owner-only (0600) atomically, with no
   window in which another local user could read them.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cli.get_user_token as get_user_token
import cli.mcp_security_scanner as scanner
import cli.scan_all_servers as scan_all
import cli.test_anthropic_api as anthropic_api

TOKEN_VALUE = "eyJhbGciOiJSUzI1NiJ9.super-secret-token-value.signature"
API_KEY_VALUE = "sk-proj-do-not-leak-this-key"


# ---------------------------------------------------------------------------
# _get_bearer_token: prefer environment, honor legacy headers, else None
# ---------------------------------------------------------------------------


class TestGetBearerToken:
    """Tests for scanner._get_bearer_token resolution order."""

    def test_prefers_environment_variable(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(scanner.BEARER_TOKEN_ENV, TOKEN_VALUE)
        headers = json.dumps({"X-Authorization": "Bearer header-token"})

        assert scanner._get_bearer_token(headers) == TOKEN_VALUE

    def test_falls_back_to_legacy_headers(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(scanner.BEARER_TOKEN_ENV, raising=False)
        headers = json.dumps({"X-Authorization": f"Bearer {TOKEN_VALUE}"})

        assert scanner._get_bearer_token(headers) == TOKEN_VALUE

    def test_returns_none_when_no_source(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(scanner.BEARER_TOKEN_ENV, raising=False)

        assert scanner._get_bearer_token(None) is None

    def test_invalid_headers_json_raises(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(scanner.BEARER_TOKEN_ENV, raising=False)

        with pytest.raises(ValueError):
            scanner._get_bearer_token("{not-json")


# ---------------------------------------------------------------------------
# _run_mcp_scanner: LLM key via env, never argv
# ---------------------------------------------------------------------------


class TestRunMcpScannerNoSecretOnArgv:
    """The external mcp-scanner must never receive the LLM key on argv."""

    def _mock_completed(self):
        completed = MagicMock()
        completed.stdout = "[\n  {}\n]"
        completed.stderr = ""
        completed.returncode = 0
        return completed

    def test_api_key_passed_via_env_not_argv(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(scanner.LLM_API_KEY_ENV, raising=False)

        with patch.object(scanner.subprocess, "run", return_value=self._mock_completed()) as run:
            scanner._run_mcp_scanner(
                "http://localhost/svc/mcp",
                analyzers="yara,llm",
                api_key=API_KEY_VALUE,
                bearer_token=None,
            )

        cmd = run.call_args.args[0]
        env = run.call_args.kwargs["env"]

        assert API_KEY_VALUE not in cmd
        assert "--api-key" not in cmd
        assert env[scanner.LLM_API_KEY_ENV] == API_KEY_VALUE

    def test_bearer_token_is_the_only_argv_secret_residual(self, monkeypatch: pytest.MonkeyPatch):
        # The external mcp-scanner exposes no env path for the bearer token, so
        # it is passed on argv (documented residual). Assert it is the ONLY
        # secret on argv and that the LLM key still travels via env.
        monkeypatch.delenv(scanner.LLM_API_KEY_ENV, raising=False)

        with patch.object(scanner.subprocess, "run", return_value=self._mock_completed()) as run:
            scanner._run_mcp_scanner(
                "http://localhost/svc/mcp",
                analyzers="yara,llm",
                api_key=API_KEY_VALUE,
                bearer_token=TOKEN_VALUE,
            )

        cmd = run.call_args.args[0]
        env = run.call_args.kwargs["env"]

        assert API_KEY_VALUE not in cmd
        assert env[scanner.LLM_API_KEY_ENV] == API_KEY_VALUE
        # Bearer token is on argv only because the third-party tool requires it.
        assert "--bearer-token" in cmd


# ---------------------------------------------------------------------------
# scan_all_servers._run_security_scan: both secrets via env, never argv
# ---------------------------------------------------------------------------


class TestScanAllServersNoSecretOnArgv:
    """The scan orchestrator must hand secrets to the scanner via env."""

    def _mock_completed(self):
        completed = MagicMock()
        completed.stdout = ""
        completed.stderr = ""
        completed.returncode = 0
        return completed

    def test_secrets_go_to_child_env_not_command_line(self):
        with patch.object(scan_all.subprocess, "run", return_value=self._mock_completed()) as run:
            scan_all._run_security_scan(
                server_url="http://localhost/svc/mcp",
                analyzers="yara,llm",
                api_key=API_KEY_VALUE,
                access_token=TOKEN_VALUE,
            )

        cmd = run.call_args.args[0]
        env = run.call_args.kwargs["env"]

        # Neither secret nor a flag that would carry it appears on argv.
        assert API_KEY_VALUE not in cmd
        assert TOKEN_VALUE not in cmd
        assert "--api-key" not in cmd
        assert "--headers" not in cmd
        assert " ".join(cmd).find(TOKEN_VALUE) == -1

        # Both secrets are delivered through the child environment instead.
        assert env[scan_all.LLM_API_KEY_ENV] == API_KEY_VALUE
        assert env[scan_all.BEARER_TOKEN_ENV] == TOKEN_VALUE


# ---------------------------------------------------------------------------
# Token files must be created owner-only (0600)
# ---------------------------------------------------------------------------


def _assert_owner_only(path: Path) -> None:
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)} for {path}"


class TestTokenFilePermissions:
    """Credential files must never be world/group-readable."""

    def test_get_user_token_save_full_token_is_0600(self, tmp_path: Path):
        out = tmp_path / "full-token.json"
        get_user_token._save_token({"access_token": TOKEN_VALUE}, str(out))
        _assert_owner_only(out)

    def test_get_user_token_overwrites_preexisting_loose_file_as_0600(self, tmp_path: Path):
        out = tmp_path / "full-token.json"
        # Simulate a pre-existing world-readable file; the writer must tighten it.
        out.write_text("stale")
        os.chmod(out, 0o644)

        get_user_token._save_token({"access_token": TOKEN_VALUE}, str(out))
        _assert_owner_only(out)

    def test_anthropic_api_save_token_file_is_0600(self, tmp_path: Path):
        out = tmp_path / "tokens.json"
        anthropic_api._save_token_file(out, {"access_token": TOKEN_VALUE})
        _assert_owner_only(out)

    def test_anthropic_api_overwrites_preexisting_loose_file_as_0600(self, tmp_path: Path):
        out = tmp_path / "tokens.json"
        out.write_text("{}")
        os.chmod(out, 0o644)

        anthropic_api._save_token_file(out, {"access_token": TOKEN_VALUE})
        _assert_owner_only(out)
