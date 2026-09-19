# rocq-lsp

`rocq-lsp` brings [VsRocq](https://github.com/rocq-prover/vsrocq)'s persistent,
incremental proof checking to the command line and MCP. It helps with two tasks:

1. **Repeated proof debugging in the middle of a `.v` file.** Edit a tactic and
   check again, reusing the unchanged checked prefix instead of executing it
   from scratch on every attempt.
2. **Inspecting proof context.** See hypotheses and goals before and after a
   tactic, together with error locations, using `rocq-lsp goal FILE:LINE`.

Each fresh `rocq compile` invocation reloads dependencies and checks the source
from the beginning. `rocq-lsp` drives VsRocq's language server `vsrocqtop` directly
over LSP, making its cached proof states available to shell users and coding
agents without VS Code.

Inspired by [lean-lsp-mcp](https://github.com/oOo0oOo/lean-lsp-mcp), it offers the
same proof tools through a CLI and an MCP server. Checking happens in memory;
building compiled library artifacts remains the job of Rocq's compiler and the
project's build system.

A background session keeps the prover warm between commands. You start and stop
it explicitly; working commands never do, so nothing is left running behind your
back.

Use `goal` to inspect a proof or check a file. It executes top to bottom up to
the line you ask about and reuses what is already checked, so ask for a file's
last line to check the whole file. It keeps the checked prefix when tactics are
inserted, deleted or replaced. If VsRocq's proof cache skips an edit, the client
restarts the prover and rechecks the current text; the project's other documents
reopen on next use.

`goal` reports every error up to that line, with the source line, column range
and a caret marker, next to the proof context.

## Assumptions

`rocq-lsp` is for fast proof repair in a development that already builds. The
prover checks only the file you are working on; everything it imports must be
compiled already. Before debugging a proof, make sure the `.vos` files of its
dependencies exist, for example with `make vos` or
`rocq-lsp build path/to/dep.vos`. A missing one makes the `Require` fail, and a
stale one is loaded as is.

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

For a full worked session, see [tutorial/Tutorial.md](./tutorial/Tutorial.md).

Positions are 1-indexed and written `FILE:LINE` or `FILE:LINE:COLUMN`.

```sh
# Open a session first; it holds the prover until you stop it.
rocq-lsp start                  # optionally: rocq-lsp start path/to/project

# The main tool: proof state at a line, showing what the tactic there did.
rocq-lsp goal src/proof/foo.v:42

# Check a whole file: ask for its last line.
rocq-lsp goal src/proof/foo.v:120

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
`find`. `diagnostics` also works but can lose incremental checking; use `goal`
on the last line instead.

## Tests

```sh
uv run --extra dev pytest tests -q
```

The CLI tests run real commands through a private daemon; the MCP tests drive a real
stdio session. Both skip when `vsrocqtop` is missing.

## Layout

```
src/               the `rocq_lsp_mcp` package
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
