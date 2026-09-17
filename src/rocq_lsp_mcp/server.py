"""MCP server exposing the same tools as the `rocq-lsp` command line.

The tool bodies live in `tools.py`; this module only adapts them to MCP.
Most agents should prefer the CLI, which needs no client configuration.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import List, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.utilities.logging import configure_logging, get_logger

from rocq_lsp_mcp import tools
from rocq_lsp_mcp.instructions import INSTRUCTIONS
from rocq_lsp_mcp.rocq_client import RocqLSPError
from rocq_lsp_mcp.workspace import Workspace

_LOG_LEVEL = os.environ.get("ROCQ_LOG_LEVEL", "INFO")
configure_logging("CRITICAL" if _LOG_LEVEL == "NONE" else _LOG_LEVEL)
logger = get_logger(__name__)


@dataclass
class AppContext:
    workspace: Workspace


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    context = AppContext(workspace=Workspace())
    try:
        yield context
    finally:
        logger.info("Closing provers")
        context.workspace.close_all()


mcp = FastMCP(name="Rocq LSP", instructions=INSTRUCTIONS, lifespan=app_lifespan)


def _ws(ctx: Context) -> Workspace:
    return ctx.request_context.lifespan_context.workspace


def _run(ctx: Context, func, /, **kwargs) -> str:
    """Call a tool, turning prover failures into readable text."""
    try:
        return func(_ws(ctx), **kwargs)
    except RocqLSPError as exc:
        return str(exc)


@mcp.tool("rocq_goal")
def rocq_goal(
    ctx: Context, file_path: str, line: int, column: Optional[int] = None
) -> str:
    """Get the proof state at a line in a Rocq file.

    THE MAIN TOOL for proof work. Only the sentences up to that point are
    executed, so it is far cheaper than checking the whole file. With no
    column, shows the state before and after the line, which is how you see
    what a tactic did. Errors up to the requested position include their
    source line, column range, and caret marker.

    Args:
        file_path (str): Abs path to the .v file.
        line (int): Line number (1-indexed).
        column (int, optional): Column (1-indexed). Defaults to before and after the line.
    """
    return _run(ctx, tools.goal, file_path=file_path, line=line, column=column)


@mcp.tool("rocq_diagnostic_messages")
def rocq_diagnostic_messages(ctx: Context, file_path: str) -> str:
    """Check a whole file and return errors, warnings, and unsolved goals at errors.

    The first call on a file costs a full compile. Prefer `rocq_goal` while
    iterating on one proof.

    Args:
        file_path (str): Abs path to the .v file.
    """
    return _run(ctx, tools.diagnostics, file_path=file_path)


@mcp.tool("rocq_file_outline")
def rocq_file_outline(ctx: Context, file_path: str) -> str:
    """List a file's imports and declarations with their line numbers.

    Token efficient; the best first look at an unfamiliar file.

    Args:
        file_path (str): Abs path to the .v file.
    """
    return _run(ctx, tools.outline, file_path=file_path)


@mcp.tool("rocq_suggest_lemmas")
def rocq_suggest_lemmas(
    ctx: Context, file_path: str, line: int, column: Optional[int] = None,
    max_results: int = 40,
) -> str:
    """Suggest lemmas that apply to the goal at a line.

    Rocq's own goal-directed suggestion engine, ranked by how well each
    lemma matches. Use it when stuck on the next step.

    Args:
        file_path (str): Abs path to the .v file.
        line (int): Line number (1-indexed), inside a proof.
        column (int, optional): Column (1-indexed). Defaults to end of line.
        max_results (int, optional): How many to return. Defaults to 40.
    """
    return _run(
        ctx, tools.suggest, file_path=file_path, line=line, column=column,
        max_results=max_results,
    )


@mcp.tool("rocq_query")
def rocq_query(
    ctx: Context, file_path: str, line: int, command: str, pattern: str
) -> str:
    """Run `Check`, `Print`, `About` or `Locate` against the state at a line.

    Args:
        file_path (str): Abs path to the .v file.
        line (int): Line number (1-indexed).
        command (str): One of Check, Print, About, Locate.
        pattern (str): The term or name to ask about.
    """
    return _run(
        ctx, tools.query, file_path=file_path, line=line, command=command,
        pattern=pattern,
    )


@mcp.tool("rocq_search")
def rocq_search(
    ctx: Context, file_path: str, query: str, line: Optional[int] = None,
    max_results: int = 30,
) -> str:
    """Run Rocq's `Search` over everything loaded at a line.

    The argument form matters: `"app_nil"` (quoted) matches names,
    `(_ ++ nil = _)` matches statement shapes, and a bare `app_nil_r` finds
    lemmas mentioning that constant.

    Args:
        file_path (str): Abs path to the .v file.
        query (str): Search argument.
        line (int, optional): Line (1-indexed) whose environment to search.
        max_results (int, optional): Cap on results. Defaults to 30.
    """
    return _run(
        ctx, tools.search, file_path=file_path, query_text=query, line=line,
        max_results=max_results,
    )


@mcp.tool("rocq_hover_info")
def rocq_hover_info(ctx: Context, file_path: str, line: int, column: int) -> str:
    """Get the type and full name of the symbol at a position.

    Args:
        file_path (str): Abs path to the .v file.
        line (int): Line number (1-indexed).
        column (int): Column (1-indexed), on the symbol rather than after it.
    """
    return _run(ctx, tools.hover, file_path=file_path, line=line, column=column)


@mcp.tool("rocq_declaration_file")
def rocq_declaration_file(ctx: Context, file_path: str, line: int, column: int) -> str:
    """Find the source file declaring the symbol at a position.

    Resolves only to compiled libraries with installed sources.

    Args:
        file_path (str): Abs path to the .v file.
        line (int): Line number (1-indexed).
        column (int): Column (1-indexed).
    """
    return _run(ctx, tools.declaration, file_path=file_path, line=line, column=column)


@mcp.tool("rocq_multi_attempt")
def rocq_multi_attempt(
    ctx: Context, file_path: str, line: int, snippets: List[str]
) -> str:
    """Try several one-line tactics at a line and compare the results.

    Each snippet replaces the line in the prover's copy only; the file on
    disk is never modified.

    Args:
        file_path (str): Abs path to the .v file.
        line (int): Line number (1-indexed) to replace.
        snippets (List[str]): One-line tactics, indented as they should appear.
    """
    return _run(ctx, tools.try_snippets, file_path=file_path, line=line, snippets=snippets)


@mcp.tool("rocq_run_code")
def rocq_run_code(ctx: Context, code: str, rocq_project_path: str = "") -> str:
    """Check a self-contained Rocq snippet and return its diagnostics.

    Args:
        code (str): Complete Rocq source, including its own Require lines.
        rocq_project_path (str, optional): Project root supplying the load path.
    """
    return _run(ctx, tools.run_code, code=code, project_path=rocq_project_path)


@mcp.tool("rocq_local_search")
def rocq_local_search(ctx: Context, query: str, limit: int = 20, project_root: str = "") -> str:
    """Confirm a declaration exists, by name, in project and stdlib sources.

    Fast, and the way to avoid inventing lemma names.

    Args:
        query (str): Name or fragment of one.
        limit (int, optional): Max matches. Defaults to 20.
        project_root (str, optional): Where to search.
    """
    return _run(ctx, tools.find, name=query, limit=limit, project_path=project_root)


@mcp.tool("rocq_file_contents")
def rocq_file_contents(ctx: Context, file_path: str, annotate: bool = True) -> str:
    """Read a Rocq file, optionally with line numbers.

    Use sparingly; prefer `rocq_file_outline`.

    Args:
        file_path (str): Abs path to the .v file.
        annotate (bool, optional): Prefix each line with its number.
    """
    return _run(ctx, tools.contents, file_path=file_path, annotate=annotate)


@mcp.tool("rocq_project_status")
def rocq_project_status(ctx: Context) -> str:
    """Show live provers, their open documents and their memory."""
    return _run(ctx, tools.status)


@mcp.tool("rocq_close_file")
def rocq_close_file(ctx: Context, file_path: str) -> str:
    """Drop a document's prover state to free memory.

    Args:
        file_path (str): Abs path to the .v file.
    """
    return _run(ctx, tools.close, file_path=file_path)


@mcp.tool("rocq_build")
def rocq_build(
    ctx: Context, target: str = "", rocq_project_path: str = "", jobs: int = 4
) -> str:
    """Build the project with `make`, then reload the prover.

    Args:
        target (str, optional): Make target, e.g. `src/proof/foo.vo`.
        rocq_project_path (str, optional): Project root.
        jobs (int, optional): Parallel jobs. Defaults to 4.
    """
    return _run(ctx, tools.build, target=target, project_path=rocq_project_path, jobs=jobs)
