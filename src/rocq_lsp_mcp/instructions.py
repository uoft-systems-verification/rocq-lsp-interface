INSTRUCTIONS = """## General Rules
- All line and column numbers are 1-indexed.
- This MCP never edits files. Make edits with your own tools, then re-check.
- Work iteratively: small steps, `admit`/`Admitted.` as placeholders, frequent checks.
- The first check of a file costs a full compile; later checks reuse the prover
  and are fast, as long as your edit is below what was already checked.

## Key Tools
(The same tools are on the command line as `rocq-lsp goal FILE:LINE` and so on.)
- rocq_file_outline: Declarations in a file with line numbers. Token efficient, start here.
- rocq_goal: Proof state at a line. THE MAIN TOOL, use often. Only checks up to that
  line, so it is much cheaper than checking the whole file.
- rocq_diagnostic_messages: Errors and warnings for the whole file.
- rocq_suggest_lemmas: Lemmas that apply to the goal at a line. Excellent for finding
  the next step; this is Rocq's own goal-directed suggestion engine.
- rocq_search: Rocq's `Search` over the loaded environment, by name or pattern.
- rocq_local_search: Confirm a declaration exists in the project or stdlib sources. Fast.
- rocq_query: `Check`, `Print`, `About` and `Locate` against the state at a line.
- rocq_hover_info: Type and full name of the symbol under a position.
- rocq_multi_attempt: Try several one-line tactics at a line and compare the results.
"""
