# A file edited from correct to incorrect keeps reporting "checks cleanly"

Status: **confirmed, not yet fixed.** Root cause identified and isolated to
VsRocq, not to this project's own logic.

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
| A sentence **deleted** from a proof body | **no** |
| Text deleted mid-sentence in a proof body (what `break.sh proof` does) | **no** |

Everything outside a proof body is handled. Changes *within* a `Proof … Qed`
block are unreliable, and the unreliable cases are exactly what proof repair
consists of.

## Root cause

After `textDocument/didChange`, vsrocqtop updates the text it holds but keeps
the affected proof's sentences marked as already executed.

Confirmed directly by asking the server for its own view with
`prover/documentState` after the edit:

- the server's document contains the **new** text, so the edit was received and
  applied correctly;
- its sentences are still listed as `(executed)`.

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

The most likely mechanism is VsRocq's scheduling of `Proof … Qed` as a single
opaque block keyed by its opening statement: change the statement and the block
is invalidated, change only its interior and it is not. This matches every row
of the table except the deletion case, so the precise rule is not fully pinned
down.

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

## The fix that will work

Replace the server process for the document and check again. That is the only
way to be certain the answer describes the file on disk.

Doing that on every edit would cost a full check each time, which is the whole
speedup. The intended design is to pay it only when needed:

1. Count real execution activity during a check, meaning `updateHighlights`
   with a non-empty processing or prepared range.
2. If the file changed since the last check and **no** execution activity was
   seen, the answer is stale by definition.
3. In that case restart the prover, reopen the document and check again.

Statement and definition edits keep the fast path, because the server really
does re-execute for those. Proof-body edits fall back to a cold check, which is
what correctness costs here. On `Combine.v` a cold check is 6.5 s, against 0.0 s
for the stale answer and 6.5 s for `rocq compile`.

Scaffolding for this is in `rocq_client.py` already: an `_executions` counter, a
`dirty` flag per document, and a `_restart` method. None of it is wired into the
check path yet, so current behaviour is unchanged and the 38 tests still pass.

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
