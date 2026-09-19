# Contributing

## Setup and test

```bash
pip install -r requirements.txt
python audit.py --selftest
```

CI runs on Python 3.10 and 3.12 and does three things — reproduce all three
locally before opening a PR:

```bash
pip install -r requirements.txt
python -m compileall -q .      # every file byte-compiles cleanly
python audit.py --selftest     # 15 offline checks, no network
```

There is no `pytest`/`unittest` suite and no separate `tests/` directory —
`selftest()` in `audit.py` (around line 566) *is* the test suite. It runs
entirely offline against inline fixtures, so it has to stay that way: don't
make it depend on network access.

There is no linter wired into CI (no ruff/flake8 configuration in this
repo) — `compileall` is the only automated style gate. Keep changes
consistent with the surrounding code rather than reformatting wholesale.

## Structure

`audit.py` is a single file. It covers, roughly in this order: crawler-access
resolution against `robots.txt` (the ten named agents — GPTBot, ClaudeBot,
PerplexityBot, and so on), readability without JavaScript, structured-data
detection (JSON-LD, including `@graph`), answerability signals, trust
signals, `llms.txt` parsing, then `--batch`/CSV/JSON output and the CLI
itself (`main()`, around line 704). Keep new checks in the section they
belong to rather than appending to the end of the file.

## Adding a new check

A new signal (a new crawler agent name, a new structured-data type, a new
answerability shape) starts with a failing case added to `selftest()` that
encodes the input and the expected finding — run `python audit.py
--selftest` and see it fail — before the detection code is written. Once it
passes, check it against the two sample sites in the README (`stripe.com`,
`nytimes.com`) or another real site to make sure the new check doesn't fire
on pages that shouldn't trigger it.

If the check changes the 0–100 score, say so explicitly in the PR — the
score is a heuristic and a silent shift in what it rewards is easy to miss
in review.

## Commit style

Match the existing log (`git log --oneline`): `Area: what changed`, lower
case after the colon, imperative, no trailing period, no conventional-commit
prefixes. Examples from this repository:

```
llms.txt: read the file, not just its existence
Run the tests in CI on every push
audit: batch mode with a one-row-per-site CSV summary
```

## Pull requests

If a change affects what counts as a blocker, a warning, or a note, update
the "What it checks" or "Honest limits" section of the README in the same
PR. Small, focused PRs; name the site(s) you tested against.
