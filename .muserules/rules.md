# Packey working rules

## Script delivery — never corrupt a heredoc
1. Prefer file tools over shell heredocs. Heredoc is a fallback, not the default.
2. Never nest code fences: a ```python fence inside an outer ``` fence closes the outer block early and the shell receives garbage. If the prompt is fenced, attach the script as a file instead.
3. Unavoidable heredoc: one flat fence, `<<'TERMINATOR'` (quoted — no expansion), unique terminator alone on its line with no whitespace (e.g. PYEOF_7XQ2, never bare EOF); the body must not contain that line.
4. Verify after writing: `wc -l` against the expected count, then `python3 -m py_compile` before executing. Wrong line count = mangled = rewrite, don't run.

## How to work
1. One numbered step per task; fresh task per step. Read only the target line range + imports block + call sites found via grep.
2. Verify every anchor exists before editing. A missing or different anchor = STOP and report, don't guess.
3. Keep the diff smallest. No drive-by refactors, renames, or reformatting.
4. Refactor sessions only: move code verbatim — same names, same behavior.

## Verification (every change)
1. `python3 -m py_compile` on touched files, then `pytest tests/ -q` must pass.
2. After editing webapp/index.html, run the JS parse check (`osascript -l JavaScript`), not just pytest — tests are not a parser.
3. For persisted state, assert the artifact (e.g. the file is actually ciphertext on disk), not just an in-memory round-trip.
4. After any edit, re-read the edited region AND the line immediately after it — past incidents were tails lost at edit boundaries.

## Hygiene
1. Mask tracking numbers in all output (first4...last4). Never print full numbers, chat IDs, keys, or tokens.
2. Never restart the bot. Report "restart required: yes/no" instead.
3. If tests import from main and break, re-export from main rather than editing many tests.
4. End with a report: the key changed lines verbatim, test counts, restart required.
