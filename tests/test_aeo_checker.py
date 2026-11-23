"""
Tests for scripts/aeo_checker.py (deterministic AEO structure checks).
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from aeo_checker import (  # noqa: E402
    UnsafeURLError,
    _assert_public_host,
    analyze_html,
    is_question,
    measure_length,
)

GOOD_ANSWER = (
    "Yes. Desert Literacy, a registered 501(c)(3) nonprofit in Phoenix, Arizona, accepts "
    "student volunteers aged 16 and older as reading tutors. Volunteers commit 2 hours per "
    "week for at least 10 weeks, receive 6 hours of free training, and can request a signed "
    "service-hours letter for school or college applications."
)


def _page(body: str, head: str = "") -> str:
    return f"<html><head><title>T</title>{head}</head><body><main>{body}</main></body></html>"


# --- question detection ----------------------------------------------------

@pytest.mark.parametrize("text", [
    "Can students volunteer at Desert Literacy?",
    "How do I apply",
    "What is the minimum age",
    "學生可以去當志工嗎",
    "鳳凰城有哪些教育類非營利組織",
])
def test_is_question_true(text):
    assert is_question(text)


@pytest.mark.parametrize("text", ["Programs", "Our Mission", "Volunteer eligibility", ""])
def test_is_question_false(text):
    assert not is_question(text)


# --- answer length ---------------------------------------------------------

def test_measure_length_english_in_range():
    m = measure_length(GOOD_ANSWER)
    assert m["unit"] == "words"
    assert m["in_ideal_range"]


def test_measure_length_chinese_uses_characters():
    text = "可以。沙漠識字協會是鳳凰城登記的 501(c)(3) 非營利組織,接受 16 歲以上的學生擔任閱讀家教志工," \
           "每週服務 2 小時、至少 10 週,並提供 6 小時免費培訓,可申請服務時數證明。"
    m = measure_length(text)
    assert m["unit"] == "cjk_chars"
    assert m["in_ideal_range"]


# --- full page analysis ----------------------------------------------------

def test_strong_answer_has_no_issues():
    html = _page(f"<h1>Can students volunteer at Desert Literacy?</h1><p>{GOOD_ANSWER}</p>")
    r = analyze_html(html, entity="Desert Literacy")
    q = r["question_sections"][0]
    assert q["answer"]["issues"] == []
    assert q["answer"]["answer_first"]
    assert q["answer"]["names_entity"]


def test_weak_opener_and_missing_fact_flagged():
    html = _page("<h1>Topic</h1><h2>Do I need experience?</h2>"
                 "<p>It depends on the role and what you are interested in doing with us.</p>")
    r = analyze_html(html, entity="Desert Literacy")
    a = r["question_sections"][0]["answer"]
    assert a["weak_opener"] == "it"
    assert not a["has_specific_fact"]
    assert a["names_entity"] is False


def test_list_first_section_is_not_answer_first():
    html = _page("<h1>Volunteer</h1><h2>How do I apply?</h2><ul><li>Fill the form</li></ul>")
    a = analyze_html(html)["question_sections"][0]["answer"]
    assert a["answer_first"] is False
    assert not a["present"]


def test_nav_headings_are_ignored():
    html = ("<html><body><nav><h2>What is this menu?</h2></nav>"
            "<main><h1>About us</h1><p>Text.</p></main></body></html>")
    assert analyze_html(html)["question_section_count"] == 0


def test_faq_schema_must_match_visible_text():
    ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"FAQPage",'
          '"mainEntity":[{"@type":"Question","name":"Do I need experience?","acceptedAnswer":{"@type":"Answer","text":"No."}},'
          '{"@type":"Question","name":"Secret question?","acceptedAnswer":{"@type":"Answer","text":"x"}}]}]}</script>')
    html = _page("<h1>Volunteer</h1><h2>Do I need experience?</h2><p>No experience is needed.</p>", head=ld)
    r = analyze_html(html)
    assert r["faq_schema"]["present"]
    assert r["faq_schema"]["not_visible_on_page"] == ["Secret question?"]
    assert r["structure_score_components"]["faq_schema"] == 5


def test_invalid_jsonld_reported():
    html = _page("<h1>X</h1>", head='<script type="application/ld+json">{bad json</script>')
    assert any("Invalid JSON-LD" in i for i in analyze_html(html)["page_issues"])


def test_multiple_h1_flagged():
    html = _page("<h1>One</h1><h1>Two</h1>")
    r = analyze_html(html)
    assert r["structure_score_components"]["single_h1"] == 0
    assert any("2 <h1>" in i for i in r["page_issues"])


def test_freshness_from_time_tag():
    html = _page('<h1>X</h1><p>Updated <time datetime="2026-11-01">Nov 1</time></p>')
    assert analyze_html(html)["freshness_signals"]


def test_score_bounds():
    html = _page(f"<h1>Can students volunteer at Desert Literacy?</h1><p>{GOOD_ANSWER}</p>")
    s = analyze_html(html, entity="Desert Literacy")["structure_score"]
    assert 0 <= s <= 100


# --- SSRF guard -------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "file:///etc/passwd",
    "ftp://example.com/",
])
def test_rejects_non_public_urls(url):
    with pytest.raises(UnsafeURLError):
        _assert_public_host(url)


def test_accepts_public_address():
    fake = [(None, None, None, None, ("93.184.215.14", 443))]
    with patch("aeo_checker.socket.getaddrinfo", return_value=fake):
        _assert_public_host("https://example.org/")
