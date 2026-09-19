# Security Policy

ai-visibility-audit fetches whatever site you point it at (or a list of
sites, in `--batch` mode) and parses the HTML, `robots.txt`, JSON-LD, and
`llms.txt` it gets back. That means the input is always attacker-influenceable
if the site itself is hostile or compromised — the tool has to survive a page
that was built to break it, not just a normal one.

## What counts as a vulnerability here

- A crafted response (HTML, `robots.txt`, JSON-LD, or `llms.txt`) that gets
  ai-visibility-audit to execute code, rather than being parsed as inert
  text/markup.
- **Server-side request forgery beyond the requested host.** A redirect, or
  a link listed in `llms.txt`, that points at a loopback, link-local, or
  private-network address (including a cloud metadata endpoint) and whose
  response then shows up in the audit report.
- **CSV/formula injection.** A site title, URL, or issue description that,
  once written into `--csv`/`--batch` output, opens as a spreadsheet formula
  (a cell starting with `=`, `+`, `-`, or `@`) instead of plain text.
- A response crafted to cause catastrophic backtracking or unbounded memory
  use in the HTML/JSON-LD/`llms.txt` parsing (a regular-expression or
  recursion denial-of-service).

Report these.

## What is not a vulnerability

- A site scoring lower or higher than you expected on the 0–100 scale, or a
  finding you disagree with — the score is a heuristic judgment call, not a
  correctness guarantee. Open a normal bug report with the URL and the
  finding you think is wrong.
- `llms.txt` link sampling stopping at the `--llms-links` default of 25 —
  documented, and raising it is one flag away.
- A site that deliberately blocks AI crawlers being reported as blocked —
  that's the intended finding, not a false positive.
- No detection of whether an AI assistant actually mentions or cites the
  audited site — explicitly out of scope ("No citation tracking" in the
  README); that needs live querying of each assistant, a different tool
  entirely.

## Reporting a vulnerability

Preferred: open a report through
[GitHub Private vulnerability reporting](https://github.com/dkautomation23/ai-visibility-audit/security/advisories/new)
on this repository.

Alternative: email **hello@dkautomation.dev** with `ai-visibility-audit` in
the subject line.

Please include:
- the Python version and OS,
- the exact command you ran,
- the site or a minimal reproducing HTML/robots.txt/llms.txt snippet (a
  small local file served with `python -m http.server` is fine if the real
  site is private).

**First response within 3 business days.** After triage we'll tell you the
expected timeline for a fix and credit you in the release notes, if you want
that.

## Supported versions

This project does not yet publish tagged releases — only the latest commit
on `main` is supported. Please confirm the issue reproduces there (and that
`python audit.py --selftest` passes) before reporting.
