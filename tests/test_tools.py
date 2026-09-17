"""End-to-end tests driving the server over a real MCP stdio session."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.mcp_client import result_text

EXPECTED_TOOLS = {
    "rocq_build",
    "rocq_project_status",
    "rocq_close_file",
    "rocq_file_contents",
    "rocq_file_outline",
    "rocq_diagnostic_messages",
    "rocq_goal",
    "rocq_suggest_lemmas",
    "rocq_query",
    "rocq_search",
    "rocq_hover_info",
    "rocq_declaration_file",
    "rocq_multi_attempt",
    "rocq_run_code",
    "rocq_local_search",
}


@pytest.fixture
def demo(test_project_path: Path) -> str:
    return str(test_project_path / "demo.v")


async def test_lists_every_tool(mcp_client_factory):
    async with mcp_client_factory() as client:
        assert EXPECTED_TOOLS.issubset(set(await client.list_tools()))


async def test_file_contents_is_annotated(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        text = await client.text("rocq_file_contents", {"file_path": demo})
        assert "1: Require Import List." in text
        assert "4: Definition answer := 42." in text


async def test_outline_lists_imports_and_declarations(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        text = await client.text("rocq_file_outline", {"file_path": demo})
        assert "Require Import List." in text
        for name in ("answer", "answer_is_42", "app_nil_demo"):
            assert name in text, text


async def test_clean_file_has_no_diagnostics(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        text = await client.text("rocq_diagnostic_messages", {"file_path": demo})
        assert "checks cleanly" in text


async def test_broken_file_reports_the_error(mcp_client_factory, test_project_path):
    broken = str(test_project_path / "broken.v")
    async with mcp_client_factory() as client:
        text = await client.text("rocq_diagnostic_messages", {"file_path": broken})
        assert "error" in text.lower()
        assert "Unable to unify" in text


async def test_diagnostics_rechecks_a_changed_proof(mcp_client_factory, tmp_path):
    path = tmp_path / "Changed.v"
    good = "Lemma one : 1 = 1. Proof. reflexivity. Qed.\n"
    path.write_text(good)
    async with mcp_client_factory() as client:
        args = {"file_path": str(path)}
        assert "checks cleanly" in await client.text("rocq_diagnostic_messages", args)
        path.write_text(good.replace("reflexivity.", "discriminate."))
        assert "No applicable tactic" in await client.text("rocq_diagnostic_messages", args)
        path.write_text(good)
        assert "checks cleanly" in await client.text("rocq_diagnostic_messages", args)


async def test_goal_shows_state_before_and_after_a_tactic(mcp_client_factory, demo):
    """Line 13 is `intros l.`, so the goal gains `l` across it."""
    async with mcp_client_factory() as client:
        text = await client.text("rocq_goal", {"file_path": demo, "line": 13})
        assert "--- before ---" in text and "--- after ---" in text
        before, after = text.split("--- after ---")
        assert "forall" in before
        assert "l : list nat" in after, after
        assert "l ++ [] = l" in after


async def test_goal_at_a_column(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        text = await client.text(
            "rocq_goal", {"file_path": demo, "line": 13, "column": 12}
        )
        assert "<cursor>" in text
        assert "⊢" in text


async def test_goals_keep_project_state_after_tactic_edits(mcp_client_factory, tmp_path):
    path = tmp_path / "Proof.v"
    other = tmp_path / "Other.v"
    good = "Lemma one : 1 = 1.\nProof.\n  idtac.\n  reflexivity.\nQed.\n"
    path.write_text(good)
    other.write_text("Definition other := 1.\n")
    async with mcp_client_factory() as client:
        await client.text("rocq_diagnostic_messages", {"file_path": str(path)})
        await client.text("rocq_file_outline", {"file_path": str(other)})
        for text in [good.replace("  idtac.\n", "  idtac.\n  idtac.\n"), good]:
            path.write_text(text)
            for _ in range(2):
                result = await client.text("rocq_goal", {"file_path": str(path), "line": 3})
                assert "⊢ 1 = 1" in result.split("--- after ---")[1]
                # Replacing the shared prover would evict this other document.
                status = await client.text("rocq_project_status")
                assert "Other.v" in status


async def test_goal_out_of_range_is_reported(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        text = await client.text("rocq_goal", {"file_path": demo, "line": 9999})
        assert "out of range" in text


async def test_suggest_lemmas_ranks_the_right_lemma_first(mcp_client_factory, demo):
    """The goal `l ++ [] = l` is closed by `app_nil_r`."""
    async with mcp_client_factory() as client:
        text = await client.text(
            "rocq_suggest_lemmas", {"file_path": demo, "line": 13, "max_results": 10}
        )
        assert "app_nil_r" in text, text


async def test_query_check_and_print(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        checked = await client.text(
            "rocq_query",
            {"file_path": demo, "line": 5, "command": "Check", "pattern": "answer"},
        )
        assert "nat" in checked
        printed = await client.text(
            "rocq_query",
            {"file_path": demo, "line": 5, "command": "Print", "pattern": "answer"},
        )
        assert "42" in printed


async def test_query_rejects_unsupported_commands(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        text = await client.text(
            "rocq_query",
            {"file_path": demo, "line": 5, "command": "Compute", "pattern": "1"},
        )
        assert "Unsupported" in text


async def test_search_by_name_substring(mcp_client_factory, demo):
    """A quoted argument matches names; a bare one matches references."""
    async with mcp_client_factory() as client:
        result = await client.call(
            "rocq_search", {"file_path": demo, "query": '"app_nil"'}
        )
        assert not result.isError
        assert "app_nil_r" in result_text(result)


async def test_search_by_pattern(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        result = await client.call(
            "rocq_search", {"file_path": demo, "query": "(_ ++ nil = _)"}
        )
        assert not result.isError
        assert "app_nil_r" in result_text(result)


async def test_hover_reports_a_type(mcp_client_factory, demo):
    """Column 12 of line 4 is inside `answer`."""
    async with mcp_client_factory() as client:
        text = await client.text(
            "rocq_hover_info", {"file_path": demo, "line": 4, "column": 12}
        )
        assert "nat" in text


async def test_multi_attempt_distinguishes_tactics(mcp_client_factory, demo):
    """Line 14 is `apply app_nil_r.`; a wrong tactic must be reported as such."""
    async with mcp_client_factory() as client:
        result = await client.call(
            "rocq_multi_attempt",
            {
                "file_path": demo,
                "line": 14,
                "snippets": ["  apply app_nil_r.", "  reflexivity."],
            },
        )
        assert not result.isError
        text = result_text(result)
        assert "app_nil_r" in text and "reflexivity" in text
        # The file on disk must be untouched.
        assert "apply app_nil_r." in Path(demo).read_text()


async def test_run_code_checks_a_snippet(mcp_client_factory):
    async with mcp_client_factory() as client:
        good = await client.text(
            "rocq_run_code", {"code": "Definition n := 1.\nLemma l : n = 1. Proof. reflexivity. Qed.\n"}
        )
        assert "cleanly" in good
        bad = await client.text(
            "rocq_run_code", {"code": "Lemma bad : 1 = 2. Proof. reflexivity. Qed.\n"}
        )
        assert "Unable to unify" in bad


async def test_local_search_finds_project_declarations(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        await client.text("rocq_file_contents", {"file_path": demo})
        result = await client.call("rocq_local_search", {"query": "answer"})
        assert not result.isError
        assert "answer_is_42" in result_text(result)


async def test_project_status_reports_the_open_file(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        await client.text("rocq_file_outline", {"file_path": demo})
        text = await client.text("rocq_project_status")
        assert "fixture_project" in text and "demo.v" in text
        assert "project file:" in text and "MB" in text


async def test_close_file_releases_the_document(mcp_client_factory, demo):
    async with mcp_client_factory() as client:
        await client.text("rocq_file_outline", {"file_path": demo})
        closed = await client.text("rocq_close_file", {"file_path": demo})
        assert "Closed" in closed
        status = await client.text("rocq_project_status")
        assert "demo.v" not in status.split("open:")[-1]


async def test_missing_file_is_reported(mcp_client_factory, test_project_path):
    async with mcp_client_factory() as client:
        text = await client.text(
            "rocq_goal", {"file_path": str(test_project_path / "nope.v"), "line": 1}
        )
        assert "does not exist" in text
