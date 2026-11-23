#!/usr/bin/env python3
"""
AEO (Answer Engine Optimization) checker.

Deterministic, no-AI checks of how well a page is structured to be lifted as a
direct answer by Google featured snippets / AI Overviews, voice assistants and
LLM answer engines. Designed for two callers:

  1. The /geo aeo skill (Claude Code) -- run from the command line.
  2. A web backend (e.g. a paid "diagnose my site" feature) -- import
     `safe_fetch_html()` and `analyze_html()` directly.

The numbers this script produces describe page STRUCTURE only. They are not a
search ranking and not a measured AI citation rate.

Usage:
    python aeo_checker.py <url> [<url> ...] [--entity "Org Name"]
    python aeo_checker.py --html-file page.html [--url https://example.org/x] [--entity "Org Name"]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Required packages not installed. Run: pip install -r requirements.txt")
    sys.exit(1)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_REDIRECTS = 5
TIMEOUT = 20

# Ideal direct-answer length (the first paragraph after a question heading).
ANSWER_MIN_WORDS, ANSWER_MAX_WORDS = 40, 60
# Equivalent range for Chinese/Japanese text, counted in characters.
ANSWER_MIN_CJK, ANSWER_MAX_CJK = 60, 120

QUESTION_WORDS_EN = (
    "what", "how", "why", "when", "where", "who", "whom", "whose", "which",
    "can", "could", "is", "are", "does", "do", "did", "should", "will",
    "would", "may", "has", "have",
)
QUESTION_MARKERS_CJK = ("嗎", "什麼", "什么", "如何", "哪些", "哪裡", "哪里", "怎麼", "怎么", "為什麼", "为什么", "是否", "多少")

WEAK_OPENERS = (
    "it ", "it's", "this ", "these ", "they ", "that ", "those ",
    "great question", "if you've ever", "if you have ever", "in today's",
    "let's", "lets ", "welcome", "have you ever", "as you may know",
    "there are many", "when it comes to",
)

CJK_RE = re.compile(r"[㐀-鿿豈-﫿぀-ヿ]")
DIGIT_RE = re.compile(r"\d")


# --------------------------------------------------------------------------- #
# Safe fetching (SSRF-guarded -- required if users can submit arbitrary URLs)  #
# --------------------------------------------------------------------------- #

class UnsafeURLError(ValueError):
    pass


def _assert_public_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Only http/https URLs are allowed (got {parsed.scheme!r}).")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no hostname.")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURLError(f"Refusing to fetch non-public address {ip} for host {host!r}.")


def safe_fetch_html(url: str, timeout: int = TIMEOUT) -> tuple[str, str, int]:
    """Fetch a public URL. Returns (final_url, html, status_code).

    Every redirect hop is re-checked so a public URL cannot bounce to an
    internal address. Responses larger than MAX_BYTES are cut off.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _assert_public_host(current)
        resp = requests.get(
            current,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                break
            current = urljoin(current, location)
            continue
        chunks, size = [], 0
        for chunk in resp.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_BYTES:
                break
            chunks.append(chunk)
        resp.close()
        encoding = resp.encoding or "utf-8"
        return current, b"".join(chunks).decode(encoding, errors="replace"), resp.status_code
    raise UnsafeURLError(f"Too many redirects starting from {url!r}.")


# --------------------------------------------------------------------------- #
# Analysis helpers                                                            #
# --------------------------------------------------------------------------- #

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_question(text: str) -> bool:
    t = _clean(text).lower()
    if not t:
        return False
    if t.endswith("?") or t.endswith("？"):
        return True
    if any(m in t for m in QUESTION_MARKERS_CJK):
        return True
    first = re.split(r"[\s,:]+", t, maxsplit=1)[0]
    return first in QUESTION_WORDS_EN


def measure_length(text: str) -> dict:
    cjk = len(CJK_RE.findall(text))
    words = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-\.%$]*", text))
    is_cjk = cjk > words
    if is_cjk:
        in_range = ANSWER_MIN_CJK <= cjk <= ANSWER_MAX_CJK
        too_long = cjk > ANSWER_MAX_CJK
    else:
        in_range = ANSWER_MIN_WORDS <= words <= ANSWER_MAX_WORDS
        too_long = words > ANSWER_MAX_WORDS
    return {"words": words, "cjk_chars": cjk, "unit": "cjk_chars" if is_cjk else "words",
            "in_ideal_range": in_range, "too_long": too_long}


def _walk_jsonld(node, out: list) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, out)
    elif isinstance(node, dict):
        out.append(node)
        for key in ("@graph", "mainEntity", "hasPart"):
            if key in node:
                _walk_jsonld(node[key], out)


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def extract_jsonld(soup: BeautifulSoup) -> tuple[list[dict], list[str]]:
    nodes, errors = [], []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            _walk_jsonld(json.loads(raw), nodes)
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid JSON-LD block: {exc.msg} (line {exc.lineno})")
    return nodes, errors


def extract_sections(soup: BeautifulSoup) -> list[dict]:
    """Split the main content into heading-led sections."""
    body = soup.find("main") or soup.find("article") or soup.body or soup
    sections, current = [], None
    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "ul", "ol", "table", "dl"]):
        if el.find_parent(["nav", "footer", "header", "aside"]):
            continue
        if el.name in ("h1", "h2", "h3", "h4"):
            current = {"level": int(el.name[1]), "heading": _clean(el.get_text(" ")),
                       "first_paragraph": None, "has_list": False, "has_table": False,
                       "first_block": None}
            sections.append(current)
            continue
        if current is None:
            continue
        if current["first_block"] is None:
            current["first_block"] = el.name
        if el.name == "p" and current["first_paragraph"] is None:
            text = _clean(el.get_text(" "))
            if text:
                current["first_paragraph"] = text
        elif el.name in ("ul", "ol", "dl"):
            current["has_list"] = True
        elif el.name == "table":
            current["has_table"] = True
    return sections


def evaluate_answer(text: str | None, entity: str | None) -> dict:
    if not text:
        return {"present": False, "issues": ["No paragraph directly after the heading."]}
    lowered = text.lower()
    length = measure_length(text)
    issues = []
    weak = next((w.strip() for w in WEAK_OPENERS if lowered.startswith(w)), None)
    if weak:
        issues.append(f"Opens with '{weak}' -- the answer should not depend on the heading or earlier text.")
    if length["too_long"]:
        issues.append("Answer paragraph is too long to be lifted as a snippet; lead with a 40-60 word answer.")
    elif not length["in_ideal_range"]:
        issues.append("Answer paragraph is very short; it may not be a complete answer.")
    has_fact = bool(DIGIT_RE.search(text))
    if not has_fact:
        issues.append("No specific number, date or amount in the answer.")
    names_entity = None
    if entity:
        names_entity = entity.lower() in lowered
        if not names_entity:
            issues.append(f"Answer does not name '{entity}', so it is not self-contained when quoted.")
    return {"present": True, "text": text, **length, "weak_opener": weak,
            "has_specific_fact": has_fact, "names_entity": names_entity, "issues": issues}


def analyze_html(html: str, url: str | None = None, entity: str | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        if t.name == "script" and re.search(r"ld\+json", t.get("type", ""), re.I):
            continue
        t.decompose()

    jsonld, jsonld_errors = extract_jsonld(soup)
    visible_text = _clean(soup.get_text(" ")).lower()

    h1s = [_clean(h.get_text(" ")) for h in soup.find_all("h1")]
    sections = extract_sections(soup)

    question_sections = []
    for s in sections:
        if s["level"] in (1, 2, 3) and is_question(s["heading"]):
            ev = evaluate_answer(s["first_paragraph"], entity)
            ev["answer_first"] = s["first_block"] == "p"
            if s["first_block"] and s["first_block"] != "p":
                ev.setdefault("issues", []).append(
                    f"Section starts with a <{s['first_block']}> instead of a direct-answer paragraph.")
            question_sections.append({"heading": s["heading"], "level": s["level"],
                                      "has_list": s["has_list"], "has_table": s["has_table"],
                                      "answer": ev})

    # FAQPage schema and its consistency with visible content
    faq_nodes = [n for n in jsonld if "FAQPage" in _types(n)]
    faq_questions, faq_not_visible = [], []
    for node in faq_nodes:
        entities = node.get("mainEntity", [])
        entities = entities if isinstance(entities, list) else [entities]
        for q in entities:
            if not isinstance(q, dict):
                continue
            name = _clean(str(q.get("name", "")))
            ans = q.get("acceptedAnswer", {})
            ans_text = _clean(BeautifulSoup(str(ans.get("text", "")), "lxml").get_text(" ")) if isinstance(ans, dict) else ""
            faq_questions.append({"question": name, "answer": ans_text})
            if name and name.lower() not in visible_text:
                faq_not_visible.append(name)

    # Freshness signals
    dates = []
    for n in jsonld:
        for key in ("dateModified", "datePublished"):
            if isinstance(n.get(key), str):
                dates.append({"source": f"JSON-LD {key}", "value": n[key]})
    for m in soup.find_all("meta", attrs={"property": re.compile(r"(article:modified_time|og:updated_time)")}):
        if m.get("content"):
            dates.append({"source": m["property"], "value": m["content"]})
    for t in soup.find_all("time"):
        if t.get("datetime"):
            dates.append({"source": "<time datetime>", "value": t["datetime"]})

    all_types = sorted({t for n in jsonld for t in _types(n)})
    speakable = any("speakable" in n for n in jsonld)

    # ---- Structure score (0-100). Structure only; not a ranking. ----
    n_q = len(question_sections)
    answered = [q for q in question_sections if q["answer"].get("present")]
    def ratio(pred):
        return (sum(1 for q in answered if pred(q["answer"], q)) / n_q) if n_q else 0.0
    components = {
        "question_headings": min(n_q, 5) / 5 * 20,
        "answer_first": ratio(lambda a, q: a.get("answer_first") and not a.get("weak_opener")) * 30,
        "answer_length": ratio(lambda a, q: a.get("in_ideal_range")) * 15,
        "specific_facts": ratio(lambda a, q: a.get("has_specific_fact")) * 10,
        "single_h1": 5 if len(h1s) == 1 else 0,
        "faq_schema": (10 if faq_questions and not faq_not_visible else 5 if faq_questions else 0),
        "freshness_signal": 10 if dates else 0,
    }
    structure_score = round(sum(components.values()))

    page_issues = []
    if len(h1s) != 1:
        page_issues.append(f"Page has {len(h1s)} <h1> tags; use exactly one, phrased as the main question or topic.")
    if n_q == 0:
        page_issues.append("No question-style headings; answer engines match headings to user questions.")
    if not faq_questions:
        page_issues.append("No FAQPage JSON-LD. Google limits FAQ rich results, but AI systems still parse it.")
    if faq_not_visible:
        page_issues.append(f"{len(faq_not_visible)} FAQ schema question(s) are not visible on the page; schema must match visible content.")
    if not dates:
        page_issues.append("No machine-readable date (dateModified, <time datetime>); add a visible 'last updated' date.")
    if jsonld_errors:
        page_issues.extend(jsonld_errors)

    return {
        "url": url,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entity": entity,
        "title": _clean(soup.title.get_text()) if soup.title else None,
        "h1": h1s,
        "question_section_count": n_q,
        "question_sections": question_sections,
        "faq_schema": {"present": bool(faq_questions), "questions": faq_questions,
                       "not_visible_on_page": faq_not_visible},
        "schema_types": all_types,
        "speakable": speakable,
        "freshness_signals": dates,
        "structure_score": structure_score,
        "structure_score_components": {k: round(v, 1) for k, v in components.items()},
        "page_issues": page_issues,
        "note": "Structure score measures page format only. It is not a search ranking or an AI citation rate.",
    }


def check_url(url: str, entity: str | None = None) -> dict:
    try:
        final_url, html, status = safe_fetch_html(url)
    except (UnsafeURLError, requests.RequestException) as exc:
        return {"url": url, "error": str(exc)}
    result = analyze_html(html, final_url, entity)
    result["status_code"] = status
    if final_url != url:
        result["requested_url"] = url
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Deterministic AEO structure checker.")
    p.add_argument("urls", nargs="*", help="One or more public URLs to check.")
    p.add_argument("--entity", help="Organization name every direct answer should mention.")
    p.add_argument("--html-file", help="Analyze a local HTML file instead of fetching.")
    p.add_argument("--url", help="URL to report for --html-file.")
    args = p.parse_args(argv)

    if args.html_file:
        with open(args.html_file, encoding="utf-8", errors="replace") as fh:
            results = [analyze_html(fh.read(), args.url or args.html_file, args.entity)]
    elif args.urls:
        results = [check_url(u, args.entity) for u in args.urls]
    else:
        p.print_help()
        return 1

    print(json.dumps(results[0] if len(results) == 1 else results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
