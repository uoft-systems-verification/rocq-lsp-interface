"""Goal lookup must preserve diagnostics even when the lookup fails."""

from unittest.mock import Mock

from rocq_lsp_mcp.rocq_client import RocqLSPError
from rocq_lsp_mcp.tools import _diagnostics_with_goals


def test_goal_lookup_failure_keeps_original_error():
    client = Mock()
    client.get_file_content.return_value = "bad."
    client.goals_at.side_effect = RocqLSPError("prover exited")
    diagnostic = {
        "severity": 1, "message": "original error",
        "range": {"start": {"line": 0, "character": 0},
                  "end": {"line": 0, "character": 3}},
    }
    result = _diagnostics_with_goals(client, "Proof.v", [diagnostic, diagnostic])
    assert len(result) == 2
    for text in result:
        assert "original error" in text
        assert "Proof goals unavailable: prover exited" in text
    client.goals_at.assert_called_once()


def test_warnings_do_not_trigger_goal_queries():
    client = Mock()
    client.get_file_content.return_value = "Check nat."
    result = _diagnostics_with_goals(client, "Proof.v", [{"severity": 2, "message": "warning"}])
    assert "warning" in result[0]
    assert "goals" not in result[0]
    client.goals_at.assert_not_called()
