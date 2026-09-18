# rocq-lsp

Rocq prover tools for a coding agent, on the command line.

Drives [VsRocq](https://github.com/rocq-prover/vsrocq)'s language server `vsrocqtop`
over LSP, so an agent can inspect goals, check files and try tactics in a Rocq
development. A mirror of
[lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp), exposed as a CLI first and
as an MCP server second.

A background session keeps the prover warm between commands. You start and stop
it explicitly; working commands never do, so nothing is left running behind your
back.

Edits sync incrementally. `goal` keeps the checked prefix when later tactics are
inserted, deleted or replaced, so inspecting an edited tactic does not replay the
file. If VsRocq's proof cache skips an edit, the client restarts the prover and
rechecks the current text; the project's other documents reopen on next use.
`diagnostics` never relies on `goal`'s partial checks, so a stale cached `Qed`
still gets a full recheck.

Errors carry the source line, column range and a caret marker. `diagnostics`
adds the unsolved goals at each error, excluding unfocused ones. `goal` reports
errors up to the requested position, including earlier ones that block it,
without checking the whole file.

## Requirements

- `vsrocqtop`, from the `vsrocq-language-server` opam package.
- `rocq` on PATH, for `build`.

Developed against Rocq 9.1 and VsRocq 2.4.3.

## Install

```sh
uv sync && uv pip install -e .
```

That puts `rocq-lsp` on PATH inside the virtualenv.

## Use

For a full worked session, see [TUTORIAL.md](./TUTORIAL.md).

Positions are 1-indexed and written `FILE:LINE` or `FILE:LINE:COLUMN`.

```sh
# Open a session first; it holds the prover until you stop it.
rocq-lsp start                  # optionally: rocq-lsp start path/to/project

# The main tool: proof state at a line, showing what the tactic there did.
rocq-lsp goal src/proof/foo.v:42

# Check a whole file.
rocq-lsp diagnostics src/proof/foo.v

# What is in this file?
rocq-lsp outline src/proof/foo.v

# Compare tactics without touching the file.
rocq-lsp try src/proof/foo.v:42 "  reflexivity." "  apply app_nil_r." "  auto."

# Check a throwaway snippet.
echo 'Lemma t : 1 = 1. Proof. reflexivity. Qed.' | rocq-lsp run-code

# Housekeeping.
rocq-lsp status                 # provers, open files, memory
rocq-lsp close src/proof/foo.v  # free a file's prover state
rocq-lsp build src/proof/foo.vo # make, then reload the prover
rocq-lsp stop                   # end the session, freeing every prover
```

`rocq-lsp --help` lists the commands grouped by what they do, with the session
commands separate from the working ones.

`rocq-lsp <command> --help` describes each one.

### Notes that save time

**Goals are cheap, whole-file checks are not.** `goal` executes only up to the line
you ask about. `diagnostics` executes the file.

**Long checks.** The CLI waits for the result with no socket response timeout,
and proof checking has no time limit by default. To interrupt a check, run
`rocq-lsp stop` from another terminal; it confirms shutdown after releasing the
session's provers. Ordinary requests remain sequential. The idle timeout does
not interrupt an active check.

For an explicit checking limit, start/restart with, for example,
`ROCQ_LSP_TIMEOUT=900 rocq-lsp restart .`. An expired check closes its prover
to stop the work and discard late responses; the next file command opens a
fresh prover. Use `ROCQ_LSP_TIMEOUT=0` for unlimited checking. Initialization,
parsing, and other protocol requests retain their separate timeouts.

**Memory.** Each open document holds prover state, often 1-4 GB for a large proof.
Watch it with `status`, release one file with `close`, and end everything with
`stop`. An idle session also exits on its own after an hour
(`ROCQ_LSP_IDLE_TIMEOUT`).

**Nothing here edits your files.** `try` changes only the prover's in-memory copy and
restores it.

### Environment

| Variable | Meaning |
|---|---|
| `ROCQ_PROJECT_PATH` | Default project root; otherwise inferred from the file. |
| `ROCQ_ARGS` | Extra Rocq options passed to the server. |
| `ROCQ_LSP_TIMEOUT` | Optional seconds to allow one check. Default 0 (unlimited). Set when starting/restarting the session. |
| `ROCQ_LSP_IDLE_TIMEOUT` | Seconds before an idle daemon exits. Default 3600. |
| `ROCQ_LSP_SOCKET` | Daemon socket path. Must stay under ~100 characters. |

Load paths come from the `_RocqProject` or `_CoqProject` above the file, the same one
the prover finds itself. Nothing to configure.

## MCP server

The same tools are available over MCP for clients that prefer it:

```json
{
  "mcpServers": {
    "rocq-lsp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/rocq-lsp-mcp", "rocq-lsp-mcp"]
    }
  }
}
```

Tool names there are `rocq_goal`, `rocq_multi_attempt`, and so on. Both front ends
call the same functions in `tools.py`.

## How it differs from lean-lsp-mcp

**There is no term goal.** Lean's `lean_term_goal` has no Rocq equivalent.

Also absent: the `gemini_*` and `gpt_*` tools, which are provider helpers rather than
prover features.

## TODO

Not yet stable, so not documented above: `search`, `suggest`, `query`, `hover`,
`find`.

## Tests

```sh
uv run pytest tests -q
```

The CLI tests run real commands through a private daemon; the MCP tests drive a real
stdio session. Both skip when `vsrocqtop` is missing.

## Layout

```
src/rocq_lsp_mcp/
  rocq_client.py   LSP client for vsrocqtop
  workspace.py     one prover per project
  tools.py         the tools, independent of how they are exposed
  daemon.py        unix socket server holding the provers
  cli.py           the `rocq-lsp` command line
  server.py        the MCP front end
```

There is no packaged Python client for Rocq the way `leanclient` exists for Lean, so
`rocq_client.py` speaks the protocol directly. Its docstring lists the VsRocq
behaviours that are easy to get wrong, including why the transport uses a reader
thread rather than polling the descriptor.
