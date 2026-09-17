# A file edited from correct to incorrect keeps reporting "checks cleanly"

Status: **fixed in the client by restarting and retrying stale checks.** The
underlying incremental invalidation bug is in VsRocq. The observations below
describe the original failure.

This is the worst kind of failure, because the wrong answer is the reassuring
one. A proof that no longer holds is reported as clean, and an agent relying on
that will believe work is finished when it is not.

## What happens

```sh
rocq-lsp start .
rocq-lsp diagnostics Combine.v      # No diagnostics; the file checks cleanly.
./break.sh proof                    # edit a tactic so the proof no longer closes
rocq-lsp diagnostics Combine.v      # No diagnostics; the file checks cleanly.   <-- wrong
rocq-lsp diagnostics Combine.v      # still wrong, and stays wrong
```

Reproduced on `rocqd/examples/vos-vok` and on a four-line synthetic file. The
real compiler disagrees: `rocq compile` on the same broken file reports
`Tactic failure: No applicable tactic.`

Checking the broken file in a **fresh** session reports the error correctly. The
fault is in the transition, not in the checking.

## Which edits are missed

Measured one edit kind at a time, each starting from a clean check:

| Edit | Re-checked? |
|---|---|
| A lemma statement, changed in place | yes |
| A definition, changed in place | yes |
| A sentence **inserted** into a proof body | yes |
| A tactic inside a proof body, changed in place | **no** |
| A one-line `Proof. … Qed.`, changed in place | **no** |
| A sentence **deleted** from a proof body | varies with sentence alignment |
| Text deleted mid-sentence in a proof body (what `break.sh proof` does) | **no** |

These were the original observations. The source investigation below explains
the dependence on opaque proof boundaries and sentence alignment. In particular,
deleting the final tactic in the small example does invalidate its `Qed`.

## Root cause

After `textDocument/didChange`, vsrocqtop can invalidate the changed tactic but
retain the successful cached **`Qed`**. The distinction matters: it is not simply
failing to notice the tactic edit.

Confirmed directly by asking the server for its own view with
`prover/documentState` after the edit:

- the server's document contains the **new** text, so the edit was received and
  applied correctly;
- the changed tactic has a new ID and is `(not executed)`;
- the unchanged `Qed` retains its ID and is `(executed)`.

For example, after replacing `reflexivity.` with `idtac.` and interpreting to the
end, the raw server reports:

```text
[8] [idtac--.] (60 -> 66) (not executed)
[7] [Qed--.]   (67 -> 71) (executed)
```

`prover/interpretToEnd` therefore finds no work to do. The message trace shows
it returning a `proofView` 2 ms later with no execution in between, and nothing
further arriving for the next five seconds:

```
0.001  highlights processed=1 processing=0 prepared=0
0.002  *** proofView          <- "finished", having run nothing
       (silence)
```

Because the client treats that `proofView` as "the check is complete", it
returns the diagnostics it already had, which are the clean ones from before the
edit. The client is reading the server correctly; the server's answer is wrong.

Manual mode never re-triggers execution afterwards, so the staleness is
permanent for that document, which is why repeating the command does not help.

The source-level mechanism is now confirmed; see the investigation below.

## What does not fix it

Each tried against the same reproduction:

| Attempt | Result |
|---|---|
| Sending `interpretToEnd` again, and a third time | still clean |
| Waiting for the re-parse to settle before interpreting | still clean |
| Removing the `documentSymbol` round trip after `didChange` | still clean |
| `interpretToPoint` above the edit, then to the end | still clean |
| `textDocument/didClose` then `didOpen` with the new text | still clean, and worse: the server keeps the **old text**, ignoring the reopen |
| Continuous mode instead of Manual | still clean |
| `prover/resetRocq` then `interpretToEnd` | **detects the error**, but on a second attempt against the larger file it took the prover down (`vsrocqtop exited with status 0`) |

`resetRocq` is the only protocol operation that clears the stale state, and it
is not safe to rely on.

## Implemented fix

Replace the server process for the document and check again. This is the current
client's conservative fallback. A server-side invalidation change can preserve
incrementality, as the experiment below demonstrates.

Doing that on every edit would cost a full check each time, which is the whole
speedup. The intended design is to pay it only when needed:

1. Track the pending edited span in the new document's UTF-16 coordinates,
   retaining the baseline across multiple edits before a check.
2. For whole-file checks, require `updateHighlights` with a processing or prepared
   range covering the edited span. Activity elsewhere is insufficient: changing
   `reflexivity.` to `discriminate.` and appending a definition was reproduced
   returning clean with activity only for the new definition. A check without
   coverage is treated as potentially stale.
   A successful point check keeps the edit pending: executing the changed tactic
   does not establish that its cached `Qed` has been revalidated.
3. In that case restart the prover, reopen the document and check again.

Point queries separately track the checked prefix. Requests before the edit or
within an already visited prefix need no new activity. Advancing through the
edit requires contiguous processing/preparation activity reaching the returned
sentence. A subsequent edit moves the boundary back as needed. Point checks
keep whole-file validation pending, and crossing a stale cached `Qed` still
triggers recovery.

Statement and definition edits keep the fast path when the server re-executes
them. Whole-file checks of proof-body edits can still fall back to a cold check.
On `Combine.v` a cold check was 6.5 s, against 0.0 s for the stale answer and
6.5 s for `rocq compile`.

Both `check_file` and `goals_at` use the shared `_interpret` path. Recovery retries
once, preserving the latest in-memory text (including temporary tactic attempts),
the requested position and timeout. The replacement clears all server caches;
the old reader is bound to its own queue so it cannot poison the new process.
Other documents reopen on demand through `Workspace.open`.

The whole-file fallback is deliberately conservative: whitespace/comment edits
can also trigger a restart. Goals before the edited span reuse the prefix. Empty documents are
handled without waiting for a proof view. Recovery failures propagate as errors.

The initial fallback incorrectly required an entire edit to execute even for a
point request before it. This caused unnecessary cold replays in the before/after
goal workflow. In `kubernetes-verification`'s `progress.v`, inserting a tactic at
line 187 improved from 6.638 s to 0.264 s, and deleting it from 6.651 s to 0.216 s,
with the same prover process retained after the fix. Edits were in memory only;
the source file was unchanged. See the [benchmark and rationale](VSROCQ-INCREMENTAL-CHECKING-SURVEY.md#follow-up-point-query-incrementality).

Regression tests cover proof edits, mixed edits with unrelated activity, multiple
pending edits, partial goals, error-to-clean recovery, deletion of all code,
the incremental fast path, and both CLI and MCP disk-edit workflows. Protocol
tests also cover unrelated document notifications and failed recovery.

## Source investigation (2026-09-17)

Compared the installed VsRocq **2.4.3** with its release source at
`6d1a104777972657be05f0af300b747ace3f94bf`. Also inspected main at
`fcefb902ba6dcf7a479b6c0f685dd128e4c60684`; main was not built or runtime-tested.

### Why the cache behaves this way

VsRocq distinguishes a theorem's exported state from the steps constructing its
opaque proof. This permits reuse after an opaque proof without replaying its
body, and supports proof skipping/delegation. In
[`scheduler.ml`](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/scheduler.ml#L178-L201),
an `OpaqueProof` task is based on the proof opener after popping the proof block.
Only this base contributes an invalidation dependency. The internal tactics do
not contribute dependencies to `Qed`.

This exclusion is explicit in the upstream
[`document: invalidate proof` test](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/tests/d_tests.ml#L55-L75):
it asserts that changing a proof step does **not** make `Qed` a dependent.
Retaining the exported state is an optimization; failing to separately recheck
the changed proof is the correctness gap for a whole-document verification tool.

The remaining pieces explain the observed behavior:

- [`document.ml`](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/document.ml#L525-L586)
  compares sentences positionally by token list. Equal sentences retain their
  IDs and cached checking results, including an unchanged `Qed`.
- [`documentManager.ml`](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/documentManager.ml#L289-L302)
  invalidates changed sentence IDs and their dependents in the old schedule.
- [`build_tasks_for`](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/language-server/dm/executionManager.ml#L493-L516)
  stops immediately on a cached successful state. Asking for the state at `Qed`
  therefore need not traverse the changed tactic.
- Inserting/deleting a sentence can shift the positional comparison, causing
  `Qed` to receive a new ID and be checked again. This explains why insertion
  benchmarks can pass while replacement fails; deletion is not uniformly broken.
- `Defined` uses the ordinary dependency chain instead of the opaque-proof path.
  The same tactic replacement under `Defined` correctly reports an unfinished
  proof in the unmodified server.

The relevant dependency exclusion, cache shortcut, and test remain in the
inspected main revision. This is source evidence, not a runtime claim about main.

### What the VS Code extension does

The extension's default settings match ours for the relevant options: Manual,
delegation `None`, and block-on-error enabled. Its
[`manualChecking.ts`](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/client/src/manualChecking.ts#L17-L35)
sends the same `interpretToPoint`, `interpretToEnd`, and stepping notifications.
There is no additional cache invalidation in those commands. In Continuous mode,
[`selection changes`](https://github.com/rocq-prover/vsrocq/blob/v2.4.3/client/src/extension.ts#L353-L361)
also send `interpretToPoint`; server-side background checking uses the same
end-of-document scheduling mechanism.

Direct protocol experiments explain how interactive use can conceal the problem:
visiting the edited tactic explicitly executes it, so `discriminate.` reports its
error. But replacing `reflexivity.` with **`idtac.`**, visiting that tactic, and
then interpreting to the end still reports no errors in the unmodified server.
The tactic succeeds with a remaining goal; the old successful `Qed` hides the
unfinished proof. The VS Code UI itself was not automated in this investigation.

This also exposed and fixed a gap in our first recovery implementation: point
checks no longer clear a pending edit before whole-document validation.

### Experimental incremental server repair

The [experimental patch](investigations/vsrocq-2.4.3-invalidation.patch) adds
invalidation edges from each opaque proof step to its terminator, while leaving
the execution base unchanged. A local build, with client recovery bypassed,
correctly handles failing tactics, unfinished proofs, point-then-end checks,
mixed edits, deletion, and restoration without restarting its process.

An execution trace confirms that the prefix is reused:

```text
Invalidating: 6                         # old tactic
Invalidating: 7                         # Qed
Non (locally) computed state 7
Reached computed state 3               # cached lemma opener / preceding context
skipping execution of already executed 4  # Proof
skipping execution of already executed 5  # simpl
```

This patch is **experimental, not installed**. It invalidates downstream
dependents too, and fails the upstream test explicitly requiring `Qed` to remain
outside the dependency set. The remaining upstream tests pass with local socket
access. A design preserving the full opaque-proof optimization should separate
cached exported state from proof-validation status and ensure dirty proofs are
checked even when the end state is cached. Merely switching to cursor requests
does not solve the unfinished-proof case.

The [protocol probe](investigations/vsrocq_stale_probe.py) reproduces the findings:

```sh
.venv/bin/python investigations/vsrocq_stale_probe.py
.venv/bin/python investigations/vsrocq_stale_probe.py --expect-fixed /path/to/patched/vsrocqtop
```

The probe deliberately bypasses our recovery path to test server behavior.

## Scope

- Affects `diagnostics`, and equally `goal`, `suggest`, `query` and `hover`,
  since all of them interpret the document through the same path. A goal shown
  after an in-place tactic edit is the goal of the previous text.
- Affects the MCP front end identically; both call the same functions.
- Very likely affects **rocqd**, the Rust daemon, which drives vsrocqtop with
  the same `didChange` + `interpretToEnd` mechanism. Its proof-repair benchmark
  inserted a line rather than editing one in place, which is one of the cases
  that does work, so the published figures are probably sound. Worth confirming
  before trusting it on in-place edits.

## Reproducing it

```sh
cd /Users/ziyu/Code/rocqd/examples/vos-vok
./break.sh none
rocq-lsp start .
rocq-lsp diagnostics Combine.v     # clean, correctly
./break.sh proof
rocq-lsp diagnostics Combine.v     # clean, incorrectly
./break.sh none
```

A minimal case without the example project:

```coq
Definition n := 1.
Lemma one : n + 0 = 1.
Proof.
  simpl.
  reflexivity.   (* change to `discriminate.` after the first clean check *)
Qed.
```
