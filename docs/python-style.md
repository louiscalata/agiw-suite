# Python style

This repository uses one formatter and one linter: Ruff, pinned in CI. The configuration lives in
`pyproject.toml` (or `ruff.toml` in repositories that are not Python packages). CI is the gate.
A local pre-commit hook is optional.

## Formatting
- `ruff format` owns layout. Do not hand-wrap against it.
- Line length: see Repository settings (120 unless stated).
- Double quotes. Four-space indentation. Magic trailing comma respected.
- One statement per line. No `;`, no `if x: y` on one line, no assigned lambdas outside tests.
- Strings whose bytes are hashed, pinned or shown to users as consent text are never reflowed by hand.
  The formatter does not change string values.

## Lint (`ruff check`)
Baseline rule set for every repository:
  E4, E7, E9, F    pyflakes and pycodestyle errors
  W                whitespace
  I                import order
  B                bugbear (likely bugs)
  UP               pyupgrade, limited by the target version
  N                pep8-naming
  C90              complexity, max-complexity 15
  RUF100           unused noqa
Per repository additions are listed in Repository settings (for example BLE, T20, S).
A `# noqa: CODE` always carries a reason: `# noqa: BLE001 - thread boundary, logged`.

## Imports
- All module-level imports at the top: standard library, third party, first party, separated by one
  blank line and sorted by Ruff (`I`).
- Optional imports go in one `try/except ImportError` block directly after the groups.
- No imports after code. Imports inside functions only to break a cycle or for a heavy optional
  dependency, with a comment saying which.
- No imports of private names (`_x`) from another module, and no `TestCase` classes imported from
  another test module.

## Naming
- snake_case modules, functions, variables; CapWords classes; UPPER_CASE for module constants that
  never change; a leading underscore for module-private names.
- Exception classes end in `Error`.
- Executables invoked by path may keep hyphenated names. Importable modules may not.
- Standard-library overrides (`setUp`, `do_GET`) and ctypes mirrors of OS structures are exempt.

## Docstrings
- PEP 257. Every module has a docstring. Every public function and class in library code has one: a
  one-line summary, then prose if needed. Closing quotes on their own line for multi-line docstrings.
- Tests and private helpers do not need docstrings.

## Type hints
- New or changed library functions are fully annotated.
- Use `from __future__ import annotations` with PEP 604 and 585 syntax (`int | None`, `list[str]`).
- Tests need no annotations.

## Errors
- Catch the specific exception you expect.
- `except Exception` is allowed only at a thread, request or process boundary. It must record or log
  the reason and carry `# noqa: BLE001 - <reason>`. A handler whose body is only `pass` needs a comment
  saying why ignoring is correct.
- Inside `except`, re-raise with `raise New(...) from err` (or `from None` on purpose).

## Output and logging
- Library modules do not `print()`. Use `logging` with one formatter per process.
  CLI entry points and scripts may print.

## Paths and subprocesses
- `pathlib.Path` for new code. Never build paths with string `+ "/"`.
- Always pass `encoding="utf-8"` to `open`, `read_text` and `write_text` for text.
- `subprocess.run([...], check=..., timeout=...)` with an argument list. Never `shell=True`, never
  `os.system`. Always state `check=` explicitly.

## State
- No `global` for mutable module state. Keep it in a small class or a lock-owning accessor that tests
  can reset.

## Size (review guidance; C90 enforces complexity)
- New functions about 60 lines or fewer, complexity 15 or less.
- New modules about 800 lines or fewer. Modules already above 1,500 lines are frozen for growth and
  are split along existing seams when touched.
- HTML, CSS and JS go in static files, not in Python string constants.

## Shared helpers
- One home per helper (finite-number check, ISO timestamp, JSON duplicate-key hook). Do not copy a
  helper into another module; import it.

## Tests
- stdlib `unittest`. Test files are named after the unit under test: `test_<module>[_<aspect>].py`.
  Never after a review round, fix number or model name (`_round2`, `_fix3`, `sol7`, `uxp12`, `grok_`).
- Shared test helpers live in a helper module (`test_support/`, `tests/support.py`), never in another
  `test_*.py`.
- Prefer behaviour assertions over asserting on exact source text. A test that reads source text
  asserts on the AST or on a token that the formatter cannot change.

## Repository settings
| Setting | Value |
|---|---|
| config file | `pyproject.toml` at the repository root |
| Ruff | `0.17.4`, required locally and pinned in `.github/workflows/source-checks.yml` (job `lint`); bump it in its own pull request |
| line-length | 120 |
| target-version | `py39` (Python 3.9 is the supported minimum) |
| extra rules | `BLE` (broad exceptions), `T20` (library output) |
| complexity | 15 |
| Python scope | `*.py` only; Python blocks in Markdown are excluded |
| excluded trees | `windows/` (separate edition), `vendor/` (pinned Nisi bundle), `android/`, `jaryo/`, `web/`; build, generated and evidence copies are excluded as recorded in `pyproject.toml` |
| per-file baseline | Existing findings are listed by exact file and rule in `pyproject.toml`, with the reason for deferral. New files are checked. Removing an existing finding removes that rule from the file's baseline. |
| change discipline | This lane uses `ruff format` only. Import sorting, lint autofixes and manual code cleanup each need their own reviewed change; the docstring-normalising AST gate must stay equal. |
| checks | `ruff check .` and `ruff format --check --diff .`; CI installs `ruff==0.17.4`. Existing root, Windows and CLI unittest suites remain the behaviour checks. |

`server.py` and `machine_state.py` retain their source bytes because JavaScript tests assert on their quote/layout spelling. The per-file lint baseline in `pyproject.toml` records the remaining rules and reasons; this lane runs no autofixes. The `monitor` aggregate includes the lint job.

Discovery is restricted to `.py` files and ignores parent Git ignore rules. The rebuild requires Ruff discovery to match its tracked, non-excluded Python manifest.
