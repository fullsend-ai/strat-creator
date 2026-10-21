import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from jira_utils import require_jira_write_auth


class TestRequireJiraWriteAuth:

    def test_returns_credentials_when_all_set(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://jira.example.com")
        monkeypatch.setenv("JIRA_USER", "user@example.com")
        monkeypatch.setenv("JIRA_TOKEN", "token123")
        s, u, t = require_jira_write_auth()
        assert s == "https://jira.example.com"
        assert u == "user@example.com"
        assert t == "token123"

    def test_exits_when_server_missing(self, monkeypatch):
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.setenv("JIRA_USER", "user@example.com")
        monkeypatch.setenv("JIRA_TOKEN", "token123")
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_user_missing(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://jira.example.com")
        monkeypatch.delenv("JIRA_USER", raising=False)
        monkeypatch.setenv("JIRA_TOKEN", "token123")
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_token_missing(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://jira.example.com")
        monkeypatch.setenv("JIRA_USER", "user@example.com")
        monkeypatch.delenv("JIRA_TOKEN", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_all_missing(self, monkeypatch):
        monkeypatch.delenv("JIRA_SERVER", raising=False)
        monkeypatch.delenv("JIRA_USER", raising=False)
        monkeypatch.delenv("JIRA_TOKEN", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_server_empty(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "")
        monkeypatch.setenv("JIRA_USER", "user@example.com")
        monkeypatch.setenv("JIRA_TOKEN", "token123")
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_server_whitespace(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "   ")
        monkeypatch.setenv("JIRA_USER", "user@example.com")
        monkeypatch.setenv("JIRA_TOKEN", "token123")
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_user_whitespace(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://jira.example.com")
        monkeypatch.setenv("JIRA_USER", "\t")
        monkeypatch.setenv("JIRA_TOKEN", "token123")
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1

    def test_exits_when_token_whitespace(self, monkeypatch):
        monkeypatch.setenv("JIRA_SERVER", "https://jira.example.com")
        monkeypatch.setenv("JIRA_USER", "user@example.com")
        monkeypatch.setenv("JIRA_TOKEN", " \t ")
        with pytest.raises(SystemExit) as exc_info:
            require_jira_write_auth()
        assert exc_info.value.code == 1
