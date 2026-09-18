"""Check what an AI assistant can actually see on a website.

    python audit.py example.com
    python audit.py example.com --pages /pricing,/about --json report.json
    python audit.py --batch sites.txt --csv summary.csv
    python audit.py --selftest

Not an SEO score. This looks at the specific things that decide whether
ChatGPT, Claude, Perplexity or Google's AI answers can read a page, understand
who published it, and quote it: crawler access, JavaScript dependence,
structured data, freshness signals and answerable copy.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

UA = "ai-visibility-audit/0.1 (+https://github.com/dkautomation23/ai-visibility-audit)"
TIMEOUT = 20

# The crawlers that feed today's assistants. Blocking one of these is the most
# common - and most invisible - reason a brand never shows up in AI answers.
AI_CRAWLERS = {
    "GPTBot": "ChatGPT training and browsing",
    "OAI-SearchBot": "ChatGPT search results",
    "ChatGPT-User": "ChatGPT when a user opens a link",
    "ClaudeBot": "Claude training and search",
    "Claude-User": "Claude when a user opens a link",
    "PerplexityBot": "Perplexity answers",
    "Google-Extended": "Gemini and AI Overviews grounding",
    "Applebot-Extended": "Apple Intelligence",
    "CCBot": "Common Crawl (feeds many models)",
    "meta-externalagent": "Meta AI",
}

# Schema.org types that actually change how an answer engine describes a business.
USEFUL_SCHEMA = {
    "Organization", "LocalBusiness", "Product", "Offer", "FAQPage", "QAPage",
    "Article", "NewsArticle", "BlogPosting", "HowTo", "Service", "Review",
    "AggregateRating", "BreadcrumbList", "WebSite", "Person",
}

QUESTION_WORDS = ("how ", "what ", "why ", "when ", "where ", "which ", "can i", "do you", "is it")


@dataclass
class Finding:
    area: str
    level: str              # blocker | warning | ok
    title: str
    detail: str
    fix: str = ""

    def as_dict(self) -> dict:
        return {"area": self.area, "level": self.level, "title": self.title,
                "detail": self.detail, "fix": self.fix}


@dataclass
class PageFacts:
    url: str
    status: int = 0
    html: str = ""
    text_chars: int = 0
    script_chars: int = 0
    title: str = ""
    description: str = ""
    canonical: str = ""
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    json_ld_types: list[str] = field(default_factory=list)
    has_author: bool = False
    has_date: bool = False
    word_count: int = 0
    question_headings: int = 0
    tables: int = 0
    lists: int = 0


def fetch(url: str) -> requests.Response | None:
    try:
        return requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None


def normalise(target: str) -> str:
    if not target.startswith(("http://", "https://")):
        target = "https://" + target
    parsed = urlparse(target)
    return f"{parsed.scheme}://{parsed.netloc}"


# ------------------------------------------------------------------ robots

def parse_robots(text: str) -> dict[str, list[str]]:
    """user-agent -> list of Disallow paths. Handles grouped user-agent lines."""
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            agent = value.lower()
            groups.setdefault(agent, [])
            current = groups[agent]
        elif key == "disallow" and current is not None:
            current.append(value)
    return groups


def crawler_status(groups: dict[str, list[str]], agent: str) -> str:
    """blocked / partly blocked / allowed, for one named crawler."""
    rules = groups.get(agent.lower())
    if rules is None:
        rules = groups.get("*")
    if rules is None:
        return "allowed"
    if any(rule == "/" for rule in rules):
        return "blocked"
    if any(rule for rule in rules):
        return "partly blocked"
    return "allowed"


def check_robots(base: str, findings: list[Finding]) -> dict:
    response = fetch(urljoin(base, "/robots.txt"))
    if response is None or response.status_code >= 400:
        findings.append(Finding("crawlers", "warning", "No robots.txt",
                                "Nothing served at /robots.txt.",
                                "Publish one - without it every crawler guesses, and some back off."))
        return {"present": False, "crawlers": {}}

    groups = parse_robots(response.text)
    statuses = {agent: crawler_status(groups, agent) for agent in AI_CRAWLERS}
    blocked = [agent for agent, state in statuses.items() if state == "blocked"]
    partly = [agent for agent, state in statuses.items() if state == "partly blocked"]

    if blocked:
        findings.append(Finding(
            "crawlers", "blocker", f"{len(blocked)} AI crawler(s) blocked outright",
            ", ".join(f"{agent} ({AI_CRAWLERS[agent]})" for agent in blocked),
            "Decide deliberately: blocking training bots is a choice, blocking search bots "
            "(OAI-SearchBot, PerplexityBot, Google-Extended) removes you from the answers.",
        ))
    if partly:
        findings.append(Finding("crawlers", "warning", f"{len(partly)} crawler(s) partly blocked",
                                ", ".join(partly), "Check the disallowed paths are not your money pages."))
    if not blocked and not partly:
        findings.append(Finding("crawlers", "ok", "All known AI crawlers can read the site", "", ""))
    return {"present": True, "crawlers": statuses}


# Two forms appear in real files. The specification shows
# `- [name](url): notes`, and plenty of sites - cursor.com among them - list
# bare URLs instead, sometimes indented into a hierarchy. Reading only the
# documented form reports a 400-link file as empty, which is a false blocker in
# a tool whose whole job is to be trusted about blockers.
LLMS_LINK = re.compile(r"^\s*-\s*\[([^\]]*)\]\(([^)]+)\)\s*(?::\s*(.*))?$")
LLMS_BARE_LINK = re.compile(r"^\s*-\s*(https?://\S+)\s*(?::\s*(.*))?$")
# How many of the listed links to actually request. A documentation index
# can list hundreds; asking for all of them is indistinguishable from a
# crawl and gets the checker throttled.
LLMS_LINK_SAMPLE = 25
LLMS_SECTION = re.compile(r"^##\s+(.+)$")


@dataclass
class LlmsTxt:
    """What a published llms.txt actually contains."""

    present: bool = False
    title: str = ""
    summary: str = ""
    sections: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)   # (text, url)
    bytes: int = 0
    lines: int = 0


def parse_llms_txt(text: str) -> LlmsTxt:
    """Read the structure the llmstxt.org specification describes.

    An H1 with the name of the project, an optional blockquote summary, then
    sections of markdown link lists. Anything else in the file is prose a model
    may read but nothing here can check.
    """
    parsed = LlmsTxt(present=True, bytes=len(text.encode("utf-8")), lines=len(text.splitlines()))
    for raw in text.splitlines():
        line = raw.rstrip()
        if not parsed.title and line.startswith("# "):
            parsed.title = line[2:].strip()
            continue
        if not parsed.summary and line.startswith("> "):
            parsed.summary = line[2:].strip()
            continue
        section = LLMS_SECTION.match(line)
        if section:
            parsed.sections.append(section.group(1).strip())
            continue
        link = LLMS_LINK.match(line)
        if link:
            parsed.links.append((link.group(1).strip(), link.group(2).strip()))
            continue
        bare = LLMS_BARE_LINK.match(line)
        if bare:
            # No title of its own; the URL is the label.
            parsed.links.append(("", bare.group(1).strip()))
    return parsed


def same_site(url: str, base: str) -> bool:
    """Is this URL part of the same site, allowing for subdomains?

    Exact host comparison calls docs.example.com linking to example.com an
    external link, which is one organisation linking to itself. This compares
    the last two labels instead - crude for a .co.uk, and right for the case
    that actually comes up.
    """
    here = urlparse(base).netloc.lower().split(":")[0]
    there = urlparse(url).netloc.lower().split(":")[0]
    if not there:
        return True                       # relative link: same site by definition
    return here.split(".")[-2:] == there.split(".")[-2:]


def check_llms_txt(base: str, findings: list[Finding], check_links: bool = True,
                   link_sample: int = LLMS_LINK_SAMPLE) -> dict:
    """Is there an llms.txt, and is it worth reading?

    Presence is the easy half and the half everyone checks. The half that
    decides whether the file does anything is the content: a file with no links
    is an index of nothing, and a link that 404s sends an assistant to a dead
    page while sounding authoritative.
    """
    response = fetch(urljoin(base, "/llms.txt"))
    if response is None or response.status_code != 200 or "#" not in response.text[:400]:
        # Deliberately a low-priority note: Google stated in May 2026 that it
        # does not use llms.txt. Anthropic and Perplexity docs do reference it.
        findings.append(Finding(
            "content map", "warning", "No llms.txt",
            "Optional and contested - Google says it does not use it; some assistants do.",
            "Cheap to add (a markdown index of your key pages), but do it after the items above.",
        ))
        return {"present": False}

    parsed = parse_llms_txt(response.text)
    detail = f"{parsed.lines} lines, {len(parsed.links)} link(s), {len(parsed.sections)} section(s)"
    findings.append(Finding("content map", "ok", "llms.txt present", detail, ""))

    if not parsed.title:
        findings.append(Finding(
            "content map", "warning", "llms.txt has no H1 title",
            "The specification opens with `# Name`; without it an assistant has no label for the file.",
            "Add a first line of `# Your product or site name`.",
        ))
    if not parsed.summary:
        findings.append(Finding(
            "content map", "warning", "llms.txt has no summary line",
            "The optional `> one sentence` blockquote is what gets quoted when the file is cited.",
            "Add `> what this is, in one sentence` under the title.",
        ))
    if not parsed.links:
        findings.append(Finding(
            "content map", "blocker", "llms.txt lists no links",
            "A content map with nothing on it is a file an assistant reads and learns nothing from.",
            "List your key pages as `- [Title](https://...): what it covers`.",
        ))
        return {"present": True, "links": 0, "broken": []}

    external = [url for _, url in parsed.links
                if url.startswith("http") and not same_site(url, base)]
    if external:
        hosts = sorted({urlparse(url).netloc for url in external})[:3]
        findings.append(Finding(
            "content map", "info",
            f"{len(external)} of {len(parsed.links)} link(s) point at another domain",
            "Mostly " + ", ".join(hosts) + ". Deliberate when it is your other property, "
            "a problem when it is not: an assistant follows the map to whoever owns those pages.",
            "Check that every domain in the map is one you control.",
        ))

    broken: list[str] = []
    if check_links:
        seen: set[str] = set()
        refused = 0
        for _, url in parsed.links:
            if len(seen) >= link_sample:
                break
            # The fragment addresses a place on a page, not a different page.
            absolute = urljoin(base, url).split("#", 1)[0]
            if absolute in seen:
                continue
            seen.add(absolute)
            answer = fetch(absolute)
            if answer is None:
                refused += 1
            elif answer.status_code >= 400:
                broken.append(f"{absolute} ({answer.status_code})")
            time.sleep(0.3)

        checked = len(seen)
        sampled = " (first %d of %d)" % (checked, len(parsed.links)) if len(parsed.links) > checked else ""

        if refused > checked / 2:
            # Being turned away by half the site says something about us, not
            # about the site. linkscan makes the same distinction for the same
            # reason: a checker that cries wolf is not read a second time.
            findings.append(Finding(
                "content map", "warning",
                "Could not verify the links in llms.txt",
                f"{refused} of {checked} requests got no answer{sampled} - most likely rate limiting, not dead pages.",
                "Re-run with a smaller --llms-links, or check from an address the site does not throttle.",
            ))
        elif broken:
            findings.append(Finding(
                "content map", "blocker", f"{len(broken)} link(s) in llms.txt are dead",
                "; ".join(broken[:5]) + (" ..." if len(broken) > 5 else "") + sampled,
                "A dead link in the file assistants trust is worse than no file.",
            ))
        else:
            findings.append(Finding(
                "content map", "ok", f"All {checked} link(s) checked in llms.txt resolve{sampled}", "", ""))

    full = fetch(urljoin(base, "/llms-full.txt"))
    if full is not None and full.status_code == 200:
        findings.append(Finding(
            "content map", "ok", "llms-full.txt present",
            f"{len(full.text.encode('utf-8')) // 1024} KB of expanded content", ""))

    return {
        "present": True,
        "title": parsed.title,
        "links": len(parsed.links),
        "sections": parsed.sections,
        "broken": broken,
    }


# ------------------------------------------------------------------ page

def read_page(url: str) -> PageFacts:
    facts = PageFacts(url=url)
    response = fetch(url)
    if response is None:
        return facts
    facts.status = response.status_code
    if response.status_code >= 400:
        return facts

    facts.html = response.text
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        if tag.name == "script":
            facts.script_chars += len(tag.get_text() or "")
        tag.decompose()

    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    facts.text_chars = len(text)
    facts.word_count = len(text.split())
    facts.title = (soup.title.get_text().strip() if soup.title else "")
    meta = soup.find("meta", attrs={"name": "description"})
    facts.description = (meta.get("content") or "").strip() if meta else ""
    canonical = soup.find("link", attrs={"rel": "canonical"})
    facts.canonical = (canonical.get("href") or "") if canonical else ""
    facts.h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    facts.h2 = [h.get_text(" ", strip=True) for h in soup.find_all("h2")]
    facts.question_headings = sum(
        1 for heading in facts.h1 + facts.h2
        if heading.strip().endswith("?") or heading.lower().startswith(QUESTION_WORDS)
    )
    facts.tables = len(soup.find_all("table"))
    facts.lists = len(soup.find_all(["ul", "ol"]))

    lowered = response.text.lower()
    facts.has_author = ('rel="author"' in lowered or '"author"' in lowered
                        or bool(soup.find("meta", attrs={"name": "author"})))
    facts.has_date = bool(soup.find("time")) or 'datepublished' in lowered or 'article:published_time' in lowered

    for block in BeautifulSoup(response.text, "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(block.get_text())
        except (ValueError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                facts.json_ld_types.extend(collect_types(item))
    return facts


def collect_types(node, depth: int = 0) -> list[str]:
    """@type values, including the ones nested inside @graph."""
    found: list[str] = []
    if depth > 4 or not isinstance(node, dict):
        return found
    value = node.get("@type")
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        found.extend(str(entry) for entry in value)
    for child in node.get("@graph", []) or []:
        found.extend(collect_types(child, depth + 1))
    for child in node.values():
        if isinstance(child, dict):
            found.extend(collect_types(child, depth + 1))
    return found


def judge_pages(pages: list[PageFacts], findings: list[Finding]) -> None:
    readable = [page for page in pages if page.status == 200]
    if not readable:
        findings.append(Finding("content", "blocker", "No page could be read",
                                "Every requested URL failed or returned an error.",
                                "Check the site is reachable without JavaScript and without a login."))
        return

    # JavaScript dependence: assistants read the HTML they are served.
    thin = [p for p in readable if p.word_count < 120]
    if thin:
        findings.append(Finding(
            "content", "blocker", f"{len(thin)} page(s) are nearly empty without JavaScript",
            ", ".join(f"{urlparse(p.url).path or '/'} ({p.word_count} words)" for p in thin),
            "Server-render or pre-render these. AI crawlers do not execute your front-end.",
        ))

    heavy = [p for p in readable if p.script_chars > max(p.text_chars * 3, 20000)]
    if heavy and not thin:
        findings.append(Finding(
            "content", "warning", "Pages are script-heavy relative to their text",
            ", ".join(urlparse(p.url).path or "/" for p in heavy),
            "Works today, but the readable part is thin - worth checking what renders server-side.",
        ))

    types = {t for page in readable for t in page.json_ld_types}
    useful = types & USEFUL_SCHEMA
    if not types:
        findings.append(Finding(
            "identity", "blocker", "No structured data at all",
            "No JSON-LD found on the pages checked.",
            "Add Organization (name, url, logo, sameAs) plus Product/Service or FAQPage. "
            "This is how an assistant states who you are without guessing.",
        ))
    elif not useful:
        findings.append(Finding("identity", "warning", "Structured data present but not the useful kinds",
                                f"found: {', '.join(sorted(types))}",
                                "Add at least Organization and one of Product / Service / FAQPage."))
    else:
        findings.append(Finding("identity", "ok", "Structured data found", ", ".join(sorted(useful)), ""))

    missing_title = [p for p in readable if not p.title]
    missing_desc = [p for p in readable if not p.description]
    if missing_title:
        findings.append(Finding("basics", "blocker", "Missing <title>",
                                ", ".join(urlparse(p.url).path or "/" for p in missing_title),
                                "One clear title per page - it is the label an answer engine quotes."))
    if missing_desc:
        findings.append(Finding("basics", "warning", f"{len(missing_desc)} page(s) without a meta description",
                                ", ".join(urlparse(p.url).path or "/" for p in missing_desc),
                                "Write a one-sentence summary; assistants reuse it as the snippet."))

    multi_h1 = [p for p in readable if len(p.h1) > 1]
    if multi_h1:
        findings.append(Finding("basics", "warning", "More than one H1",
                                ", ".join(urlparse(p.url).path or "/" for p in multi_h1),
                                "Keep one H1 so the main claim of the page is unambiguous."))

    answerable = sum(page.question_headings for page in readable)
    if answerable == 0:
        findings.append(Finding(
            "answerability", "warning", "No question-shaped headings anywhere",
            "Assistants lift answers from sections that look like answers.",
            "Add a short FAQ using the exact questions customers ask, one direct answer each.",
        ))
    else:
        findings.append(Finding("answerability", "ok", f"{answerable} question-shaped heading(s)", "", ""))

    dated = [p for p in readable if p.has_date]
    if not dated:
        findings.append(Finding("trust", "warning", "No publication or update dates",
                                "Nothing in the markup says when this was last true.",
                                "Expose a visible date and datePublished/dateModified in JSON-LD."))
    if not any(p.has_author for p in readable):
        findings.append(Finding("trust", "warning", "No author or publisher attribution",
                                "Assistants prefer sources that name who is speaking.",
                                "Name an author or the organisation as publisher in the markup."))


def score(findings: list[Finding]) -> int:
    """100 minus the weight of what is broken. Blunt on purpose."""
    penalty = sum({"blocker": 22, "warning": 7}.get(finding.level, 0) for finding in findings)
    return max(0, 100 - penalty)


def audit(target: str, extra_paths: list[str] | None = None,
          link_sample: int = LLMS_LINK_SAMPLE) -> dict:
    base = normalise(target)
    started = time.perf_counter()
    findings: list[Finding] = []

    robots = check_robots(base, findings)
    llms = check_llms_txt(base, findings, link_sample=link_sample)

    paths = ["/"] + [path.strip() for path in (extra_paths or []) if path.strip()]
    pages = [read_page(urljoin(base, path)) for path in paths]
    judge_pages(pages, findings)

    order = {"blocker": 0, "warning": 1, "ok": 2}
    findings.sort(key=lambda finding: order[finding.level])

    return {
        "site": base,
        "checked_at": time.strftime("%Y-%m-%d %H:%M"),
        "score": score(findings),
        "robots": robots,
        "llms_txt": llms,
        "pages": [{"url": p.url, "status": p.status, "words": p.word_count,
                   "schema": sorted(set(p.json_ld_types))} for p in pages],
        "findings": [finding.as_dict() for finding in findings],
        "took_seconds": round(time.perf_counter() - started, 1),
    }


def print_report(report: dict) -> None:
    mark = {"blocker": "[!]", "warning": "[~]", "ok": "[+]"}
    print(f"\n{report['site']}   AI-readability {report['score']}/100   "
          f"({report['took_seconds']}s, {len(report['pages'])} page(s))")

    crawlers = report["robots"].get("crawlers", {})
    if crawlers:
        # "partly blocked" only means some paths are disallowed - saying
        # "blocked" there would be alarming and wrong.
        blocked = [a for a, state in crawlers.items() if state == "blocked"]
        partly = [a for a, state in crawlers.items() if state == "partly blocked"]
        allowed = len(crawlers) - len(blocked) - len(partly)
        line = f"  crawlers: {allowed} allowed, {len(partly)} limited to some paths, {len(blocked)} blocked"
        if blocked:
            line += f"   ({', '.join(blocked)})"
        print(line)

    for level in ("blocker", "warning", "ok"):
        rows = [f for f in report["findings"] if f["level"] == level]
        if not rows:
            continue
        print()
        for finding in rows:
            print(f"  {mark[level]} {finding['title']}  [{finding['area']}]")
            if finding["detail"]:
                print(f"      {finding['detail'][:150]}")
            if finding["fix"] and level != "ok":
                print(f"      fix: {finding['fix'][:150]}")
    print()


def selftest() -> int:
    """Parsing and judgement are tested offline; the network part is not mocked."""
    checks, failures = 0, []

    def check(label, condition):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(label)

    robots = parse_robots("""
        User-agent: *
        Disallow: /admin/

        User-agent: GPTBot
        Disallow: /

        User-agent: PerplexityBot
        Disallow:
        # comment line
    """)
    check("robots: named block wins", crawler_status(robots, "GPTBot") == "blocked")
    check("robots: empty disallow means allowed", crawler_status(robots, "PerplexityBot") == "allowed")
    check("robots: falls back to *", crawler_status(robots, "ClaudeBot") == "partly blocked")
    check("robots: comments ignored", "# comment line" not in str(robots))

    check("url normalising adds scheme", normalise("example.com") == "https://example.com")
    check("url normalising strips path", normalise("https://example.com/pricing") == "https://example.com")

    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "X"},
        {"@type": ["Product", "Offer"]},
        {"@type": "WebSite", "publisher": {"@type": "Person"}},
    ]}
    types = set(collect_types(graph))
    check("schema types read from @graph", {"Organization", "Product", "Offer", "WebSite"} <= types)
    check("schema types read from nested nodes", "Person" in types)

    findings = [Finding("a", "blocker", "x", ""), Finding("b", "warning", "y", "")]
    check("score subtracts blockers and warnings", score(findings) == 71)
    check("score never goes negative", score([Finding("a", "blocker", "x", "")] * 10) == 0)

    empty = PageFacts(url="https://x/", status=200, word_count=12, text_chars=60, script_chars=90000)
    findings = []
    judge_pages([empty], findings)
    titles = " ".join(f.title for f in findings)
    check("empty page is a blocker", "nearly empty" in titles)
    check("missing schema is a blocker", "No structured data" in titles)
    check("missing title is caught", "Missing <title>" in titles)

    good = PageFacts(url="https://x/", status=200, word_count=800, text_chars=5000, script_chars=1000,
                     title="T", description="D", h1=["One"], h2=["How do I start?"],
                     json_ld_types=["Organization", "FAQPage"], has_author=True, has_date=True)
    findings = []
    judge_pages([good], findings)
    check("a healthy page produces no blockers", not any(f.level == "blocker" for f in findings))
    check("question headings are credited", any("question-shaped" in f.title for f in findings))

    # --- llms.txt: the structure, not just the presence ---------------------
    good = parse_llms_txt(
        "# Acme Robotics\n"
        "\n"
        "> Calibration services and spare parts for CNC lines.\n"
        "\n"
        "## Docs\n"
        "\n"
        "- [Calibration guide](https://acme.example/docs/calibration): step by step\n"
        "- [Spare parts](https://acme.example/parts)\n"
        "\n"
        "## Company\n"
        "\n"
        "- [About](https://acme.example/about): who we are\n"
    )
    check("llms.txt title is read", good.title == "Acme Robotics")
    check("llms.txt summary is read", good.summary.startswith("Calibration services"))
    check("llms.txt sections are read", good.sections == ["Docs", "Company"])
    check("llms.txt links are read", len(good.links) == 3)
    check("a link without notes still counts",
          ("Spare parts", "https://acme.example/parts") in good.links)

    check("a subdomain is the same site", same_site("https://anthropic.com/x", "https://docs.anthropic.com"))
    check("another organisation is not", not same_site("https://github.com/x", "https://docs.anthropic.com"))
    check("a relative link is the same site", same_site("/docs", "https://acme.example"))

    # cursor.com lists bare URLs, indented, with no markdown link syntax at all.
    plain = parse_llms_txt(
        "# Cursor Documentation\n"
        "\n"
        "## Get Started\n"
        "\n"
        "- https://cursor.com/docs.md\n"
        "  - https://cursor.com/docs/models/claude-opus-5.md\n"
    )
    check("bare URLs count as links", len(plain.links) == 2)
    check("an indented bare URL is still a link",
          plain.links[1][1].endswith("claude-opus-5.md"))
    check("a bare URL has no title of its own", plain.links[0][0] == "")

    bare = parse_llms_txt("# Only a title\n\nSome prose and no links at all.\n")
    check("a file with no links is seen as having none", bare.links == [])
    check("prose is not mistaken for a link", bare.sections == [])

    untitled = parse_llms_txt("> summary first\n\n- [A](https://x.example/a)\n")
    check("a missing H1 is detectable", untitled.title == "")
    check("the summary is still read without a title", untitled.summary == "summary first")

    check("byte count is of the encoded file", parse_llms_txt("# \u00c4\n").bytes == 5)

    print(f"selftest: {checks - len(failures)}/{checks} passed")
    for failure in failures:
        print("  FAILED:", failure)
    return 1 if failures else 0


def write_summary_csv(reports: list[dict], path: str) -> None:
    """One row per site - the view you want when auditing a client list."""
    import csv as csv_module

    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv_module.writer(handle)
        writer.writerow(["site", "score", "blockers", "warnings", "crawlers_blocked",
                         "llms_txt", "top_issue"])
        for report in reports:
            findings = report["findings"]
            blockers = [f for f in findings if f["level"] == "blocker"]
            crawlers = report["robots"].get("crawlers", {})
            writer.writerow([
                report["site"],
                report["score"],
                len(blockers),
                len([f for f in findings if f["level"] == "warning"]),
                len([a for a, state in crawlers.items() if state == "blocked"]),
                "yes" if report["llms_txt"].get("present") else "no",
                blockers[0]["title"] if blockers else "",
            ])
    print(f"{len(reports)} site(s) -> {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Audit what AI assistants can read on a site.")
    parser.add_argument("site", nargs="?", help="domain or URL, e.g. example.com")
    parser.add_argument("--pages", default="", help="extra paths to check, comma separated: /pricing,/about")
    parser.add_argument("--batch", metavar="FILE", help="file with one domain per line")
    parser.add_argument("--json", metavar="FILE", help="write the full report as JSON")
    parser.add_argument("--csv", metavar="FILE", help="one summary row per site (use with --batch)")
    parser.add_argument("--llms-links", type=int, default=LLMS_LINK_SAMPLE,
                        help=f"how many llms.txt links to check (default {LLMS_LINK_SAMPLE}; your own site can take more)")
    parser.add_argument("--selftest", action="store_true", help="run offline checks and exit")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    targets = []
    if args.batch:
        with open(args.batch, encoding="utf-8") as handle:
            targets = [line.strip() for line in handle
                       if line.strip() and not line.startswith("#")]
    elif args.site:
        targets = [args.site]
    else:
        parser.error("give me a domain, --batch a file, or --selftest")

    extra = args.pages.split(",") if args.pages else []
    reports = []
    for target in targets:
        report = audit(target, extra, link_sample=args.llms_links)
        reports.append(report)
        print_report(report)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(reports if len(reports) > 1 else reports[0], handle, indent=2)
        print(f"full report -> {args.json}")
    if args.csv:
        write_summary_csv(reports, args.csv)
    if len(reports) > 1:
        worst = min(reports, key=lambda report: report["score"])
        print(f"{len(reports)} site(s) checked, lowest score: {worst['site']} at {worst['score']}/100")

    # Exit 1 when something is actually blocking visibility - handy in CI.
    return 1 if any(f["level"] == "blocker" for report in reports for f in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
