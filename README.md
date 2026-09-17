# ai-visibility-audit

[![CI](https://github.com/dkautomation23/ai-visibility-audit/actions/workflows/ci.yml/badge.svg)](https://github.com/dkautomation23/ai-visibility-audit/actions/workflows/ci.yml)

Checks what an AI assistant can actually read on a website — crawler access,
JavaScript dependence, structured data, freshness, answerable copy — and prints
the specific things standing between you and being quoted.

```bash
python audit.py example.com --pages /pricing,/about
```

## Why

Gartner expects AI assistants to handle around a quarter of searches in 2026.
Meanwhile most sites were built for Google's crawler, which renders JavaScript
and infers a lot. The assistants' crawlers do neither: they fetch the HTML, and
whatever is not in it does not exist.

The usual finding is not subtle. Either the site blocks the crawlers in
`robots.txt` (often copied from a template years ago), or the page has 40 words
of HTML and 900 KB of script, or nothing in the markup says who published it.
None of that shows up in an SEO tool, because for Google the site is fine.

## What it checks

**Crawler access.** Ten agents by name — GPTBot, OAI-SearchBot, ChatGPT-User,
ClaudeBot, Claude-User, PerplexityBot, Google-Extended, Applebot-Extended,
CCBot, meta-externalagent — resolved through the actual `robots.txt` grouping
rules, with `blocked` and `limited to some paths` reported separately. Saying
"you are blocked" when a single `/admin/` rule applies would be alarmist.

**Readability without JavaScript.** Word count of the served HTML, and how much
script sits next to it. A page under 120 words is treated as a blocker.

**Identity.** JSON-LD types, including the ones nested inside `@graph`. Without
`Organization` an assistant describes your business from whatever it found
elsewhere.

**Answerability.** Question-shaped headings, tables and lists — the shapes an
answer engine can lift.

**Trust signals.** Author or publisher attribution, publication and update dates.

**llms.txt.** Reported, but as a low-priority note: Google stated in May 2026
that it does not use it, while some other assistants reference it. It is listed
last on purpose — plenty of agencies sell it as the fix, and it is not.

## Sample output

Two real sites, run today:

```console
$ python audit.py stripe.com

https://stripe.com   AI-readability 58/100   (2.2s, 1 page(s))
  crawlers: 0 allowed, 10 limited to some paths, 0 blocked

  [~] Pages are script-heavy relative to their text  [content]
  [~] More than one H1  [basics]
  [~] No question-shaped headings anywhere  [answerability]
  [~] No publication or update dates  [trust]
  [~] No author or publisher attribution  [trust]

  [+] llms.txt present  [content map]   379 lines
  [+] Structured data found  [identity]   Organization, WebSite
```

```console
$ python audit.py nytimes.com

https://nytimes.com   AI-readability 50/100
  crawlers: 0 allowed, 0 limited to some paths, 10 blocked
      (GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, Claude-User, PerplexityBot, ...)

  [!] 10 AI crawler(s) blocked outright  [crawlers]
      fix: Decide deliberately: blocking training bots is a choice, blocking search
           bots removes you from the answers.
```

The second one is a deliberate business decision, and the tool says so rather
than calling it a mistake. Most sites that look like this did not decide
anything — that is the difference worth finding.

## Checking a list of sites

```bash
python audit.py --batch samples/sites.example.txt --csv summary.csv
```

```csv
site,score,blockers,warnings,crawlers_blocked,llms_txt,top_issue
https://stripe.com,58,0,6,0,yes,
https://basecamp.com,65,0,5,0,no,
https://nytimes.com,50,1,4,10,no,10 AI crawler(s) blocked outright
```

Your site next to three competitors on one screen is a more useful argument
than any single score.

## Install

```bash
git clone https://github.com/dkautomation23/ai-visibility-audit.git
cd ai-visibility-audit
pip install -r requirements.txt
python audit.py --selftest     # 15 checks, offline
```

Python 3.10+, two dependencies (requests, beautifulsoup4). `--json report.json`
writes the full result. Exit code is `1` when a blocker is found, so it fits in
CI or a nightly check.

## Honest limits

This measures whether you *can* be read and quoted. It does not measure whether
you *are*:

- **No citation tracking.** Whether ChatGPT or Perplexity actually mentions your
  brand for a given prompt needs repeated querying of each assistant over time —
  a different job, with API costs and sampling problems.
- **Comparison is structural, not competitive.** `--batch` puts you and your
  competitors side by side on readability, but it cannot tell you which of you
  the assistants actually recommend.
- **No content work.** It will tell you the FAQ is missing; writing questions in
  your customers' words is not something a script should guess.
- **One page at a time.** It reads the URLs you pass, not the whole site.
- **Technical layer only.** The consensus is that AI visibility is mostly
  positioning, mentions and third-party authority; the technical side is the
  part that is checkable in five seconds, and the part that silently blocks
  everything else when it is wrong.

Fixing what it finds is usually a day of work on the site. Knowing whether the
fix moved anything takes measurement over weeks.

## License

MIT
