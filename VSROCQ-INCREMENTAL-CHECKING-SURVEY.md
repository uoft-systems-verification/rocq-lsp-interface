# VsRocq incremental checking: investigation and client implications

Date: 2026-09-17

## Findings

VsRocq can accept a proof-body edit while retaining the successful cached state
of its closing `Qed`. Asking it to interpret to the end can then skip the changed
proof and return no diagnostics. A fresh process detects the error.

The cache structure deliberately separates an opaque theorem's exported state
from the tactics constructing its proof. The failure is that reaching a cached
end state does not necessarily validate the edited proof body. Our client
previously treated completion of that request as whole-document verification.

The VS Code extension uses the same interpretation requests. Explicitly visiting
an edited tactic can execute it, but does not guarantee that its closing `Qed`
is checked again. There is no extra invalidation step in the extension's manual
commands that our client could simply copy.

The client now uses a conservative restart fallback. An experimental server
patch demonstrates that correctness can be recovered while retaining earlier
execution states, although that patch changes an upstream dependency invariant
and is not ready for adoption.

## Scope and evidence

| Item | Investigated version or method |
| --- | --- |
| Installed server | `vsrocq-language-server` 2.4.3, with Rocq 9.1.1 |
| Release source | `v2.4.3`, commit `6d1a104777972657be05f0af300b747ace3f94bf` |
| Newer source | Main at `fcefb902ba6dcf7a479b6c0f685dd128e4c60684` |
| Runtime experiments | Direct LSP requests, deliberately bypassing client recovery |
| Extension comparison | Inspection of its TypeScript command handlers and settings |
| Experimental repair | Local build of 2.4.3 with additional invalidation dependencies |

The VS Code UI was not automated. Main was inspected but not built or tested;
its relevant dependency exclusion, cache shortcut, and dependency test remain.
The runtime conclusions below apply to the tested 2.4.3 configuration. The
installed server was not replaced, and no upstream issue or patch was submitted.

## Why the invalidation behaves this way

For a simple opaque proof, the scheduler records two paths: the tactic sequence
inside the proof, and the exported state available after `Qed`. An `OpaqueProof`
task starts from the proof opener after the internal proof block is popped.
Its invalidation dependencies do not include the internal tactics. This supports
reuse of the theorem's exported state and proof skipping or delegation.
See the [scheduler's opaque-proof branch](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/scheduler.ml#L178-L201).

This separation is deliberate: the upstream
[`document: invalidate proof` test](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/tests/d_tests.ml#L55-L75)
explicitly expects `Qed` to remain outside a changed proof step's dependency set.
Preserving that exported state is useful, but the changed proof still needs an
independent validity check before a whole-file tool can report success.

Three implementation details explain the failure:

1. The document diff compares sentences positionally by their token lists.
   Equal sentences retain their IDs and cached checking results. An unchanged
   `Qed` can therefore retain its old success after a tactic replacement.
   [Document diff and patching](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/document.ml#L525-L586)
2. Invalidation follows changed IDs and their dependents in the old schedule.
   The missing tactic-to-terminator dependency leaves that `Qed` cached.
   [Invalidation traversal](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/documentManager.ml#L289-L302)
3. Execution planning stops when it reaches an already successful cached state.
   Interpreting to the end need not traverse the newly invalidated tactic.
   [Execution planning](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/executionManager.ml#L493-L516)

The server's document-state output after replacing `reflexivity.` with `idtac.`
and interpreting to the end makes the distinction visible:

```text
[8] [idtac--.] (60 -> 66) (not executed)
[7] [Qed--.]   (67 -> 71) (executed)
```

This corrects the initial diagnosis: the edited tactic is noticed and
invalidated. The surviving successful `Qed` is the critical stale state.

Sentence insertion or deletion can shift the positional comparison and give
`Qed` a new ID, forcing rechecking. Consequently, successful insertion tests do
not establish that replacement works, and deletion is not uniformly broken.
Transparent proofs ending with `Defined` use the ordinary dependency chain;
the corresponding unfinished-proof experiment correctly reports an error.

## What the extension does, and what the experiments show

The relevant extension defaults match our client: Manual mode, delegation
`None`, and block-on-error enabled. Its
[manual command handlers](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/client/src/manualChecking.ts#L17-L35)
send `interpretToPoint`, `interpretToEnd`, and stepping notifications directly.
In Continuous mode,
[cursor selection changes](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/client/src/extension.ts#L353-L361)
also send `interpretToPoint`. The server's background interpretation uses the
same end-of-document execution-planning mechanism.

Explicitly visiting a changed tactic can reveal a tactic failure. However,
replacing a closing tactic with `idtac.` gives a stronger counterexample:
the tactic succeeds, a goal remains, and the cached `Qed` still hides the
unfinished proof when interpretation proceeds to the end.

The following experiments started with a clean check in one process, then edited
the document and checked again. Client recovery was bypassed.

| Edit or request sequence | Unmodified 2.4.3 | Experimental server patch |
| --- | --- | --- |
| Replace `reflexivity.` with `discriminate.` | Incorrectly reports no errors | Reports tactic failure |
| Replace `reflexivity.` with `idtac.` | Incorrectly reports no errors | Reports incomplete proof |
| Visit that `idtac.`, then interpret to end | Incorrectly reports no errors | Reports incomplete proof |
| Break the tactic and append a definition | Executes the new definition but misses the proof error | Reports tactic failure |
| Delete the final tactic in the small example | Reports incomplete proof | Reports incomplete proof |
| Replace the tactic with `idtac.` under `Defined` | Reports incomplete proof | Reports incomplete proof |

All six patched cases and their restorations used the same server process.
Restoring the original text returned clean diagnostics in both versions.

The original investigation also tried repeated interpretation, extra waiting,
Continuous mode, and close/reopen without resolving the stale answer.
Close/reopen is not a reliable reset: the server retains closed documents under
its memory policy and can reuse them when reopened.
`resetRocq` detected the error in one trial but subsequently terminated the
prover on a larger example. These historical observations are recorded in
[the bug investigation](STALE-CHECK-BUG.md).

## Current client protection and its performance cost

Both CLI and MCP tools share
[`RocqLSPClient._interpret`](src/rocq_lsp_mcp/rocq_client.py).
The implementation records a pending edit span in the current text's UTF-16
coordinates, including multiple updates before checking. During whole-file
interpretation it looks for processing or preparation activity covering that span in the same
document. Missing coverage triggers one process restart and replay using the
latest in-memory text and requested position.

Point requests instead track the prefix validated since the earliest pending
edit. A request before the edit or within that prefix can reuse its cached state.
To advance through edited text, contiguous processing/preparation ranges must
reach the returned sentence. The returned sentence matters because a cursor in
whitespace can refer to the preceding sentence. Each further edit moves the
validated boundary back to its start if necessary; a failed request cannot
advance it. Crossing a cached `Qed` without covering the intervening edit still
triggers recovery. A successful point request keeps whole-file validation pending.

A global activity counter was insufficient: an appended definition can execute
while a changed proof remains unchecked. A point request also cannot clear the
pending edit merely because its tactic executed. The latter case was discovered
during this survey and added as a regression test.

The restart clears cached diagnostics and server state. Reader queues are tied
to their original processes to prevent late output contaminating a replacement.
Failures propagate as errors. Empty documents avoid waiting for a proof view.
These are defensive client measures, not repairs to the server's dependency
model; highlight activity itself is not a formal proof-validity certificate.

| Situation | Effect on incremental reuse |
| --- | --- |
| Unchanged document | Reuses existing state |
| Tested definition, statement, and insertion edits with confirming activity | Keeps the existing process |
| Edit lacking confirming activity | Replays in a fresh process |
| Point requests before the edit or within the validated prefix | Reuse cached state |
| Whole-file checks after some comment edits | May restart conservatively |
| Other documents in the same project after a restart | Lose cached state and reopen on demand |

A point replay executes only up to the requested position; a whole-file replay
checks the whole file. Repeated whole-file diagnostics after tactic repair can
therefore lose much of the original speed benefit; ordinary point queries now
preserve the prefix. The original `Combine.v` report measured about
6.5 seconds for a cold check; this survey did not repeat that benchmark.

After the additional point-then-end fix, the client suite passed **64 tests**,
including real prover checks and CLI/MCP disk-edit workflows.

### Follow-up: point-query incrementality

The initial client fallback applied the whole-edit coverage requirement to every
point query. A goal tool without a column makes two requests: before the tactic,
then after it. The first request cannot execute an edit after its cursor, so our
guard restarted a healthy incremental prover before the second request. This
performance regression was in the MCP/CLI client, independently of the upstream
stale-`Qed` problem. The prefix tracking above fixes that unnecessary restart.

Benchmarked `/Users/ziyu/Code/kubernetes-verification`, using
`src/proof/controllers/replicaset/progress.v` and the installed, unmodified
VsRocq 2.4.3. Each column is one run of the same before/after goal sequence through
the shared client; these are not MCP transport timings. Edit cases include
`update_file`. Only the private prover's in-memory document was changed.

| Request | Before fix | After fix |
| --- | --- | --- |
| Cold goal at line 187 | 4.998 s | 4.910 s |
| Unchanged warm goal at line 187 | 0.007 s | 0.007 s |
| Insert `idtac.` after line 186; goal at 187 | 6.638 s; restarted | 0.264 s; same process |
| Delete inserted tactic; goal at 187 | 6.651 s; restarted | 0.216 s; same process |
| Insert after line 186; goal at 18 | 5.928 s; restarted | 0.200 s; same process |

The exact CLI command `/usr/bin/time -p rocq-lsp goal
src/proof/controllers/replicaset/progress.v:18` was also run in an isolated
session: **6.14 s cold, 0.25 s warm**. That includes CLI overhead.
Both benchmark runs confirmed the source file's bytes remained unchanged
(SHA-256 `b189b40d2640378913e2255e12f26ecf732926f8bd17b7270512b205addb3e65`).
These runs benchmark goal inspection; they do not compile or verify the entire
Kubernetes proof file.

Reproduce with the [benchmark script](investigations/benchmark_incremental_goals.py):

```sh
.venv/bin/python investigations/benchmark_incremental_goals.py \
  /Users/ziyu/Code/kubernetes-verification \
  src/proof/controllers/replicaset/progress.v --after-line 186 --expect-incremental
```

The complete client suite now passes **71 tests**, covering insertion, deletion,
replacement, repeated point queries, subsequent edits to a visited tactic,
failed requests, MCP project-state retention, and stale-`Qed` recovery.

## Experimental server repair and the preferred direction

The [experimental patch](investigations/vsrocq-2.4.3-invalidation.patch) adds
invalidation edges from every opaque proof step to its closing terminator,
without changing the execution base. It was built locally and passed the six
direct-protocol cases above without client recovery or process replacement.

The execution trace confirmed reuse of the lemma opener and earlier proof steps:

```text
Invalidating: 6                           # old tactic
Invalidating: 7                           # Qed
Non (locally) computed state 7
Reached computed state 3                 # cached opener and preceding context
skipping execution of already executed 4 # Proof
skipping execution of already executed 5 # simpl
```

The patch is **experimental and not installed**. It also invalidates downstream
dependents, sacrificing some of the opaque-proof optimization. The upstream
suite fails the existing test that explicitly excludes `Qed` from the
dependency set; the remaining tests passed with local socket access.

The recommended long-term design is to distinguish reusable exported state from
proof-validation status. Whole-document checking must schedule dirty proofs and
validate their closure even when later exported states remain reusable. This is
a design recommendation, not an implemented or upstream-approved solution.

## Using the tools and reproducing the survey

To visit an edited tactic on line 42 and inspect its before/after states:

```sh
rocq-lsp start .
rocq-lsp goal Combine.v:42
rocq-lsp diagnostics Combine.v
```

For a single position immediately after `  idtac.` on line 42, use
`rocq-lsp goal Combine.v:42:9`. CLI positions are 1-indexed. Visiting the tactic
inspects its effect; the subsequent diagnostics command checks the whole file.
Current safeguards remain active and may restart the prover. Restart an already
running session after updating the client so it loads the new implementation.

To inspect the underlying server behavior independently of our recovery path,
run the [protocol probe](investigations/vsrocq_stale_probe.py) from this project:

```sh
.venv/bin/python investigations/vsrocq_stale_probe.py
.venv/bin/python investigations/vsrocq_stale_probe.py --expect-fixed /path/to/patched/vsrocqtop
```

The probe creates temporary documents, prints diagnostics and document states,
and checks process reuse. The `--expect-fixed` option asserts that each broken
case is diagnosed and each restoration succeeds.

Other clients using the same sequence, including `rocqd`, warrant separate
regression checks. This survey does not establish their behavior or validate
their published performance figures.
