"""Shutdown failures must remain visible to the CLI user."""

from unittest.mock import Mock

from rocq_lsp_mcp import cli


def test_stop_does_not_hide_a_failed_shutdown(monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *args: (False, "daemon disconnected"))
    output = Mock()
    monkeypatch.setattr("builtins.print", output)
    assert cli.handle_stop() == 1
    output.assert_called_once_with("daemon disconnected")
