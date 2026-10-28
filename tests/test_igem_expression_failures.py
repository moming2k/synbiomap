"""Offline tests for the expression-failure scraper.

No test in this file opens a network connection. HTML comes from
tests/fixtures/expression_failure_wiki.html, and the rate limiter is given a
fake clock (plus one very short real-clock check).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests

from src.ingest.igem_expression_failures import (
    LLMError,
    LLMExtractor,
    PageCache,
    StrictRateLimiter,
    dedupe_records,
    extract_cases,
    find_keyword_hits,
    heuristic_cases,
    html_to_paragraphs,
    page_in_scope,
    parse_llm_payload,
    records_from_html,
    wiki_url_for,
)
from src.ingest.igem_wiki import derive_crawl_scope

FIXTURE = Path(__file__).parent / "fixtures" / "expression_failure_wiki.html"


def _fixture_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _records():
    return records_from_html(
        _fixture_html(),
        team_name="Example",
        year="2019",
        url="https://2019.igem.org/Team:Example/Results",
        llm=None,
    )


# ---------------------------------------------------------------------------
# Keyword matcher
# ---------------------------------------------------------------------------

def test_keyword_matcher_is_case_insensitive_and_keeps_a_paragraph_window():
    paragraphs = [
        "The construct was cloned last week.",
        "We FAILED TO EXPRESS the enzyme in the first attempt.",
        "The culture was then discarded.",
    ]
    hits = find_keyword_hits(paragraphs, window=1)
    assert len(hits) == 1
    assert "failed to express" in hits[0].matched_terms
    # Neighbours on both sides stay with the hit.
    assert "cloned last week" in hits[0].passage
    assert "FAILED TO EXPRESS" in hits[0].passage
    assert "discarded" in hits[0].passage


def test_keyword_matcher_finds_the_required_phrases_in_fixture_html():
    paragraphs = html_to_paragraphs(_fixture_html())
    # The members link lives in <nav>, which is not part of the content root.
    assert all("Members" not in paragraph for paragraph in paragraphs)

    hits = find_keyword_hits(paragraphs, window=1)
    terms = {term for hit in hits for term in hit.matched_terms}
    assert "expression failed" in terms
    assert "inclusion body" in terms
    assert "no band" in terms
    assert "western blot negative" in terms
    assert "failed to express" in terms

    inclusion_windows = [hit for hit in hits if "inclusion body" in hit.matched_terms]
    assert any("laccase construct" in hit.passage for hit in inclusion_windows)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

def test_rate_limiter_spaces_requests_on_a_fake_clock():
    slept: list[float] = []
    now = {"t": 0.0}

    def clock():
        return now["t"]

    def sleep(seconds):
        slept.append(seconds)
        now["t"] += seconds

    limiter = StrictRateLimiter(1.0, clock=clock, sleep=sleep)
    limiter.acquire()
    limiter.acquire()
    limiter.acquire()

    # First call is free. Each later call waits out the full one-second gap.
    assert slept == [1.0, 1.0]
    assert now["t"] == 2.0


def test_rate_limiter_does_not_wait_when_time_has_already_passed():
    slept: list[float] = []
    now = {"t": 10.0}

    limiter = StrictRateLimiter(
        1.0,
        clock=lambda: now["t"],
        sleep=lambda seconds: slept.append(seconds),
    )
    limiter.acquire()
    now["t"] = 12.0  # more than min_interval later
    limiter.acquire()
    assert slept == []


def test_rate_limiter_real_clock_never_goes_faster_than_the_interval():
    limiter = StrictRateLimiter(0.05)
    started = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    elapsed = time.monotonic() - started
    # 4 starts need 3 gaps of 0.05s. The bound is a little under 0.15s so a
    # coarse timer does not fail a limiter that really did wait.
    assert elapsed >= 0.12


def test_default_interval_is_one_second():
    assert StrictRateLimiter().min_interval == 1.0


# ---------------------------------------------------------------------------
# Heuristic extractor
# ---------------------------------------------------------------------------

def test_heuristic_extracts_host_conditions_and_part_id():
    records = _records()
    matched = [row for row in records if row["sequence"] == "BBa_K123456"]
    assert matched, records
    row = matched[0]
    assert row["extraction_method"] == "heuristic"
    assert row["team_name"] == "Example"
    assert row["year"] == 2019
    assert "BL21(DE3)" in row["host"]
    assert "E. coli" in row["host"]
    assert "0.5 mM IPTG" in row["conditions"]
    assert "37°C" in row["conditions"]
    assert "LB" in row["conditions"]
    assert "inclusion" in row["failure_reason"]
    assert "expression failed" in row["result"].lower() or "inclusion" in row["result"].lower()
    assert "BBa_K123456" in row["source_snippet"]


def test_heuristic_rejects_avoidance_citation_cloning_gel_and_promoter():
    records = _records()
    blob = " ".join((row["result"] + " " + row["source_snippet"]).lower() for row in records)
    assert "avoid inclusion" not in blob
    assert "nature biotechnology" not in blob
    assert "gm cassette" not in blob
    assert "astrocyte" not in blob
    # The SDS-PAGE "no band" is a real protein-gel failure and stays.
    assert any("35 kda" in row["result"].lower() for row in records)


def test_heuristic_western_blot_and_failed_to_express_are_separate_cases():
    records = _records()
    western = [row for row in records if row["sequence"] == "BBa_K999999"]
    failed = [row for row in records if row["sequence"] == "BBa_K555555"]
    assert len(western) == 1
    assert "DH5" in western[0]["host"]
    assert western[0]["failure_reason"] == "western blot negative"
    assert len(failed) == 1
    assert failed[0]["failure_reason"] == "expression failed"
    # The shake-flask sentence must not inherit the Western blot's strain.
    assert not failed[0]["host"] or "DH5" not in failed[0]["host"]


def test_expression_failed_allows_words_between_expression_and_failed():
    # Real wiki phrasing: the verb is not glued to the noun.
    cases = heuristic_cases(
        "After IPTG induction, the protein expression of Bacillus subtilis WB800n failed."
    )
    assert len(cases) == 1
    assert "Bacillus subtilis" in cases[0]["host"]
    assert cases[0]["failure_reason"] == "expression failed"


def test_heuristic_negative_western_variant_and_raw_sequence():
    cases = heuristic_cases(
        "The negative western blot of the soluble lysate showed no product.\n\n"
        "Sequence ATGCATGCATGCATGCATGCATGCATGCATGCATGCATGCATGC failed to express in E. coli BL21."
    )
    assert any(case["failure_reason"] == "western blot negative" for case in cases)
    sequenced = [case for case in cases if case["sequence"] and case["sequence"].startswith("ATGC")]
    assert sequenced
    assert sequenced[0]["host"] == "E. coli BL21"
    assert "failed to express" in sequenced[0]["result"].lower()


# ---------------------------------------------------------------------------
# Cache does not spend a rate-limit slot or a request
# ---------------------------------------------------------------------------

def test_cache_hit_does_not_call_the_limiter_or_the_network(tmp_path):
    cache = PageCache(tmp_path)
    url = "https://2019.igem.org/Team:Example/Results"
    cache.store(url, 200, "<html><p>cached body</p></html>")

    def explode(*_args, **_kwargs):
        raise AssertionError("network should not be used")

    class ExplodingSession:
        def get(self, *_args, **_kwargs):
            explode()

    calls = {"n": 0}

    class CountingLimiter:
        def acquire(self):
            calls["n"] += 1

    html = __import__(
        "src.ingest.igem_expression_failures", fromlist=["fetch_html"]
    ).fetch_html(ExplodingSession(), url, limiter=CountingLimiter(), cache=cache)
    assert "cached body" in html
    assert calls["n"] == 0


def test_cached_404_is_not_refetched(tmp_path):
    cache = PageCache(tmp_path)
    url = "https://2019.igem.org/Team:Example/Missing"
    cache.store(url, 404, None)
    cached, html = cache.lookup(url)
    assert cached is True
    assert html is None


# ---------------------------------------------------------------------------
# LLM parser, and fallback when the call fails
# ---------------------------------------------------------------------------

def test_llm_parser_keeps_only_real_failures_and_rejects_invented_quotes():
    passage = "Expression failed and the protein accumulated as inclusion bodies in E. coli BL21(DE3)."
    payload = {
        "cases": [
            {
                "is_expression_failure": False,
                "sequence": None,
                "host": "E. coli",
                "conditions": None,
                "result": "this one is not a failure",
                "failure_reason": None,
                "source_quote": passage,
            },
            {
                "is_expression_failure": True,
                "sequence": "BBa_K123456",
                "host": "E. coli BL21(DE3)",
                "conditions": "0.5 mM IPTG",
                "result": "Expression failed and the protein accumulated as inclusion bodies.",
                "failure_reason": "protein accumulated as inclusion bodies",
                "source_quote": "Expression failed and the protein accumulated as inclusion bodies",
            },
            {
                "is_expression_failure": True,
                "sequence": None,
                "host": None,
                "conditions": None,
                "result": "A made-up result.",
                "failure_reason": None,
                "source_quote": "this quote was not in the passage",
            },
        ]
    }
    cases = parse_llm_payload(payload, passage)
    assert len(cases) == 2
    assert cases[0]["source_snippet"].startswith("Expression failed")
    # Invented quote is replaced by the passage itself.
    assert cases[1]["source_snippet"].startswith("Expression failed")
    assert "made-up" not in cases[1]["source_snippet"]


def test_llm_http_failure_falls_back_to_the_heuristic():
    passage = (
        "We expressed BBa_K123456 in E. coli BL21(DE3) with 0.5 mM IPTG at 37°C. "
        "Unfortunately expression failed and the protein accumulated as inclusion bodies."
    )

    def post(*_args, **_kwargs):
        raise requests.ConnectionError("offline")

    llm = LLMExtractor(api_key="test-key", base_url="https://example.test/v1", model="fake", post=post)
    cases, method = extract_cases(passage, llm)
    assert method == "heuristic"
    assert cases
    assert cases[0]["sequence"] == "BBa_K123456"
    assert "BL21(DE3)" in cases[0]["host"]


def test_llm_empty_answer_is_not_replaced_by_the_heuristic():
    """An empty cases list means the model looked and found nothing."""

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": '{"cases": []}'}}]}

    def post(*_args, **_kwargs):
        return Response()

    llm = LLMExtractor(api_key="test-key", base_url="https://example.test/v1", model="fake", post=post)
    passage = "Unfortunately expression failed and the protein accumulated as inclusion bodies."
    cases, method = extract_cases(passage, llm)
    assert method == "llm"
    assert cases == []


def test_llm_from_env_is_absent_without_a_key(monkeypatch):
    for name in ("IGEM_LLM_API_KEY", "OPENAI_API_KEY", "IGEM_LLM_BASE_URL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    assert LLMExtractor.from_env() is None
    assert LLMExtractor.from_env({"IGEM_LLM_API_KEY": "your_key_here"}) is None
    client = LLMExtractor.from_env({
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "http://localhost:8000/v1",
        "OPENAI_MODEL": "local-model",
    })
    assert client is not None
    assert client.model == "local-model"
    assert client.base_url.startswith("http://localhost")


def test_llm_error_type_is_raised_for_garbage_json():
    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "not json"}}]}

    llm = LLMExtractor(
        api_key="k", base_url="https://example.test/v1", model="m",
        post=lambda *_a, **_k: Response(),
    )
    with pytest.raises(LLMError):
        llm.extract("Expression failed in E. coli.")


# ---------------------------------------------------------------------------
# De-dupe, scope, URL eras
# ---------------------------------------------------------------------------

def test_dedupe_records_keeps_the_first_copy():
    row = {
        "team_name": "Example",
        "year": 2019,
        "url": "https://2019.igem.org/Team:Example/Results",
        "sequence": "BBa_K1",
        "host": "E. coli",
        "conditions": None,
        "result": "Expression failed.",
        "failure_reason": "expression failed",
        "source_snippet": "Expression failed.",
        "extraction_method": "heuristic",
    }
    other = dict(row, result="The Western blot was negative.")
    assert len(dedupe_records([row, dict(row), other])) == 2


def test_scope_keeps_notebooks_and_results_but_skips_sponsors_and_other_teams():
    host, prefix = derive_crawl_scope("https://2019.igem.org/Team:Aachen")
    assert page_in_scope("https://2019.igem.org/Team:Aachen/Notebook", host, prefix)
    assert page_in_scope("https://2019.igem.org/Team:Aachen/Results", host, prefix)
    assert not page_in_scope("https://2019.igem.org/Team:Aachen/Sponsors", host, prefix)
    assert not page_in_scope("https://2019.igem.org/Team:Other/Results", host, prefix)
    assert not page_in_scope("https://2019.igem.org/Special:Search", host, prefix)
    assert not page_in_scope("https://2019.igem.org/Team:Aachen/photo.jpg", host, prefix)
    assert not page_in_scope("https://2019.igem.org/Team:Aachen/Results?action=edit", host, prefix)


def test_wiki_url_follows_the_two_igem_eras():
    assert wiki_url_for("Aachen", 2019) == "https://2019.igem.org/Team:Aachen"
    assert wiki_url_for("GO_Paris-Saclay", 2022) == "https://2022.igem.wiki/go-paris-saclay"
    assert wiki_url_for("Shanghai high school", 2021) == (
        "https://2021.igem.org/Team:Shanghai_high_school"
    )
