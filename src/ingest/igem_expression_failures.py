"""
igem_expression_failures.py — find protein-expression failures on iGEM wikis.

What this module is for
-----------------------
iGEM teams often write up wet-lab results in which a protein did not come out
the way they planned: the culture made inclusion bodies, a Western blot was
negative, or an SDS-PAGE lane had no band at the expected size. Those write-ups
are unstructured HTML. This module turns them into one JSONL record per failure
case so a later analysis can compare hosts, induction conditions, and the
reason the expression failed.

It is a sibling of ``igem_wiki.py`` (which crawls the same wikis looking for
paper DOIs). The two crawlers share the same idea of a team wiki:

  - 2008–2021 MediaWiki, e.g. ``https://2019.igem.org/Team:Aachen``
  - 2022 onward a static site, e.g. ``https://2023.igem.wiki/aachen``

and the same rule for staying inside one team: same host, same path prefix,
no ``Special:`` / file / asset links. See ``derive_crawl_scope`` in
``igem_wiki.py``. One deliberate difference: the DOI crawler skips lab
notebooks, because notebooks rarely cite papers. Expression failures are
often written in notebooks and on Results pages, so those pages stay in
scope here. Sponsor, gallery, and judging pages are still skipped.

How a page becomes records
--------------------------
1. Fetch the HTML (strictly at most 1 request/second, cached on disk).
2. Keep a high-recall keyword screen over the visible paragraphs, with a
   short window of neighbouring paragraphs so the host and the induction
   protocol are not cut off from the sentence that says the experiment failed.
3. Normalise each candidate passage into structured fields. If an
   OpenAI-compatible API key is configured, a model does this step against a
   JSON schema. If it is not, a deterministic regex/heuristic extractor runs
   instead, so the pipeline still finishes on a laptop with no key.

What counts as a failure
------------------------
Inclusion bodies are treated as a failed *soluble* expression: the protein
was made, but as an insoluble aggregate rather than a usable soluble product
(Baneyx & Mujacic, 2004, *Nature Biotechnology*; Singh & Panda, 2005,
*Journal of Bioscience and Bioengineering*). A missing band or a negative
Western blot counts when the surrounding sentence is about a protein gel,
not when "no band" means a cloning check succeeded (for example a cassette
that was supposed to disappear). Plans "to avoid inclusion bodies" and
bibliography lines that merely cite an inclusion-body paper are not cases.

Politeness
----------
The old ``20XX.igem.org`` wikis sit behind a firewall that answers 403 if
traffic is too fast, and their robots.txt allows ordinary page views while
disallowing ``/Special:``, ``/wiki/index.php``, and query-string URLs. This
module therefore:

  - sends the same research User-Agent as ``igem_wiki.py`` (the firewall
    rejects any User-Agent containing the token "bot"),
  - never exceeds one request per second, with extra backoff on 403/429/5xx,
  - requests only team content pages (no Special pages, no query strings),
  - and caches fetched HTML so a rerun does not hit the servers again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.ingest.igem_wiki import (
    USER_AGENT,
    _SKIP_EXTENSIONS,
    _SKIP_NAMESPACES,
    _canonical,
    derive_crawl_scope,
)

logger = logging.getLogger(__name__)

# Fastest gap this scraper will allow between HTTP requests. The CLI refuses
# a rate above 1/s; tests inject a shorter interval via the clock/sleep hooks.
DEFAULT_MIN_INTERVAL = 1.0

# Paragraphs kept on either side of a keyword hit. One neighbour is enough to
# catch "we induced with IPTG" sitting just above "expression failed".
DEFAULT_WINDOW = 1

OUTPUT_FIELDS = (
    "team_name",
    "year",
    "url",
    "sequence",
    "host",
    "conditions",
    "result",
    "failure_reason",
    "source_snippet",
    "extraction_method",
)

# Sub-pages that do not describe wet-lab expression. Notebooks are absent on
# purpose (see the module docstring). Matched against the path *below* the
# team prefix, so the team landing page itself is always eligible.
_SKIP_PAGE_SUBSTR = (
    "sponsor", "acknowledg", "gallery", "photo", "judging", "medal",
    "award", "collaborat", "attribution", "human-practice", "human_practice",
    "education", "inclusiv", "entrepreneur", "partnership",
)
_SKIP_PAGE_EXACT = frozenset({
    "team", "members", "member", "roster", "contact", "sponsors", "attributions",
})

# Fetched first after the landing page. Expression write-ups live here.
_PRIORITY_HINTS = (
    "result", "experiment", "express", "purif", "protein", "character",
    "notebook", "sds", "western", "assay", "labwork", "wet-lab", "wetlab",
)

# ---------------------------------------------------------------------------
# Keyword screen (high recall). Precision is the extractor's job.
# Labels are stable names used in tests and logs; patterns are case-insensitive.
# ---------------------------------------------------------------------------

_FAILURE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # "expression failed" and "protein expression of Bacillus subtilis failed"
    # (a few words may sit between the noun and the verb).
    ("expression failed", re.compile(
        r"\b(?:protein\s+)?expression\b(?:\s+\w+){0,6}\s+fail(?:ed|ure|s|ing)?\b", re.I)),
    ("failed to express", re.compile(r"\bfail(?:ed|ure|s)?\s+to\s+express\b", re.I)),
    ("failed expression", re.compile(r"\bfailed\s+expression\b", re.I)),
    ("could not express", re.compile(r"\b(?:could|can)\s+not\s+express\b", re.I)),
    ("unable to express", re.compile(r"\bunable\s+to\s+express\b", re.I)),
    ("unsuccessful expression", re.compile(r"\bunsuccessful(?:ly)?\s+express\w*\b", re.I)),
    ("did not express", re.compile(r"\b(?:did|does|do)\s+not\s+express\b", re.I)),
    ("not expressed", re.compile(r"\b(?:was|were|is|are|been)?\s*not\s+expressed\b", re.I)),
    ("no expression", re.compile(r"\bno\s+(?:protein\s+)?expression\b", re.I)),
    ("inclusion body", re.compile(r"\binclusion[\s\-]*bod(?:y|ies)\b", re.I)),
    ("no band", re.compile(
        r"\bno\s+(?:visible\s+|clear\s+|obvious\s+|distinct\s+)?bands?\b", re.I)),
    ("band not observed", re.compile(
        r"\bbands?\s+(?:was|were|is|are)\s+not\s+(?:observed|detected|seen|visible|present)\b",
        re.I)),
    ("western blot negative", re.compile(
        r"\bwestern[\s\-]+blot(?:\s+was|\s+were|\s+is|\s+result\s+was)?\s+negative\b", re.I)),
    ("negative western", re.compile(r"\bnegative\s+western(?:[\s\-]+blot)?\b", re.I)),
)

# Patterns that are specific enough to stand on their own (once an experiment
# cue is present). The others need an extra protein-experiment word in the
# same sentence, otherwise "no band" on a PCR gel or "the promoter is not
# expressed in astrocytes" would become a protein-expression failure.
_STRONG_LABELS = frozenset({
    "expression failed", "failed to express", "failed expression",
    "could not express", "unable to express", "unsuccessful expression",
    "inclusion body", "western blot negative", "negative western",
})
_NO_BAND_LABELS = frozenset({"no band", "band not observed"})

_AVOIDANCE = re.compile(
    r"\b(?:avoid|prevent|reduce|minimize|minimise|without|rather than)\b"
    r".{0,80}inclusion",
    re.I,
)
# A real failure verb sitting next to an "avoid inclusion bodies" plan.
_HARD_FAILURE = re.compile(
    r"\b(?:failed|failure|not\s+expressed|no\s+bands?|negative|did\s+not|"
    r"could\s+not|unable|unsuccessful|not\s+detected|insoluble)\b",
    re.I,
)
# Shows the sentence is about *this team's experiment*, not a paper title.
_EXPERIMENT_CUE = re.compile(
    r"\b(?:we|our|induced|induction|iptg|sds|pellet|soluble|insoluble|"
    r"failed|failure|observed|detected|purif|lysate|culture|strain|"
    r"expressed|expression|band|blot|host|transform|accumulat|formed|"
    r"fraction|supernatant)\b",
    re.I,
)
# Extra context required for weak phrases ("not expressed", "no expression").
_PROTEIN_CONTEXT = re.compile(
    r"protein|inclusion[\s\-]*bod|sds|western|iptg|soluble|insoluble|"
    r"\bkda\b|recombinant|lysat|coomassie|his[\-\s]?tag|\bBBa_|"
    r"\bBL21\b|E\.?\s*coli|Bacillus|plasmid|vector|chromoprotein|"
    r"\benzyme\b|\bpeptide\b|\bgfp\b|\brfp\b|\bcfp\b|\byfp\b|mcherry",
    re.I,
)
_PROTEIN_GEL = re.compile(
    r"\b(?:protein|sds|western|kda|soluble|insoluble|lysat|coomassie|"
    r"inclusion|his[\-\s]?tag)\b",
    re.I,
)
_CLONING_GEL = re.compile(
    r"\b(?:\d+\s*bp|base pairs?|cassette|colony pcr|digest(?:ion)?|"
    r"plasmid backbone|restriction)\b",
    re.I,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

_BBA = re.compile(r"\bBBa_[A-Za-z0-9]+\b")
_DNA = re.compile(r"\b[ACGTUacgtu]{40,}\b")
_AA = re.compile(r"\b[ACDEFGHIKLMNPQRSTVWY]{30,}\b")

_HOST_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bE\.?\s*coli\s+BL21\s*\(\s*DE3\s*\)(?:\s*pLysS)?", re.I),
    re.compile(r"\bBL21\s*\(\s*DE3\s*\)(?:\s*pLysS)?", re.I),
    re.compile(r"\bE\.?\s*coli\s+BL21\b", re.I),
    re.compile(r"\bE\.?\s*coli\s+DH5\s*(?:α|alpha|a)\b", re.I),
    re.compile(r"\bE\.?\s*coli\s+Rosetta(?:\s*\(\s*DE3\s*\))?", re.I),
    re.compile(r"\bE\.?\s*coli\s+SHuffle(?:\s*T7)?", re.I),
    re.compile(r"\bE\.?\s*coli\b", re.I),
    re.compile(r"\bBL21\b"),
    re.compile(r"\bBacillus\s+subtilis(?:\s+\w+)?", re.I),
    re.compile(r"\bWB800n\b"),
    re.compile(r"\bS\.?\s*cerevisiae\b", re.I),
    re.compile(r"\bPichia\s+pastoris\b", re.I),
    re.compile(r"\bRosetta(?:\s*\(\s*DE3\s*\))?\b", re.I),
    re.compile(r"\bSHuffle(?:\s*T7)?\b"),
)

_BLOCK_TAGS = [
    "p", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "figcaption", "dd", "td", "th",
]


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------

class StrictRateLimiter:
    """Global minimum gap between HTTP requests.

    ``igem_wiki.Throttle`` is allowed to speed up after a quiet streak. This
    limiter never does. Every ``acquire`` waits until at least ``min_interval``
    seconds after the previous request was allowed to *start*. The default gap
    is one second (at most one request per second).

    ``clock`` and ``sleep`` are injectable so tests can check the spacing
    without waiting on a real clock. The lock is held only while the next
    slot is reserved, not while the thread sleeps, so several threads still
    share one schedule.
    """

    def __init__(self, min_interval: float = DEFAULT_MIN_INTERVAL, *,
                 clock=None, sleep=None):
        if min_interval <= 0:
            raise ValueError("min_interval must be positive")
        self.min_interval = float(min_interval)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next: float | None = None

    def acquire(self) -> None:
        """Block until this caller may send one request."""
        with self._lock:
            now = self._clock()
            if self._next is None or now >= self._next:
                wait = 0.0
                self._next = now + self.min_interval
            else:
                wait = self._next - now
                self._next = self._next + self.min_interval
        if wait > 0:
            self._sleep(wait)


# ---------------------------------------------------------------------------
# On-disk page cache
# ---------------------------------------------------------------------------

class PageCache:
    """Remember fetched pages so a rerun does not request them again.

    Files live under ``root`` (by default a folder inside ``data/raw/``, which
    is gitignored). A 200 response stores the HTML. A 404 is remembered too,
    so a dead sub-page is not requested on every run. Firewall responses
    (403/429) are not cached: those are temporary and should be retried.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> str:
        return hashlib.sha256(_canonical(url).encode("utf-8")).hexdigest()

    def _meta_path(self, url: str) -> Path:
        return self.root / f"{self._key(url)}.meta.json"

    def _html_path(self, url: str) -> Path:
        return self.root / f"{self._key(url)}.html"

    def lookup(self, url: str) -> tuple[bool, str | None]:
        """Return ``(was_cached, html)``.

        ``html`` is None for a remembered 404. ``was_cached`` is False when
        the URL should be requested.
        """
        meta_path = self._meta_path(url)
        if not meta_path.exists():
            return False, None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False, None
        status = int(meta.get("status") or 0)
        if status == 404:
            return True, None
        if status != 200:
            return False, None
        html_path = self._html_path(url)
        if not html_path.exists():
            return False, None
        return True, html_path.read_text(encoding="utf-8", errors="replace")

    def store(self, url: str, status: int, html: str | None) -> None:
        meta = {
            "url": _canonical(url),
            "status": int(status),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._meta_path(url).write_text(json.dumps(meta), encoding="utf-8")
        if status == 200 and html is not None:
            self._html_path(url).write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_html(
    session: requests.Session,
    url: str,
    *,
    limiter: StrictRateLimiter,
    cache: PageCache,
    timeout: int = 30,
    max_retries: int = 3,
    sleeper=None,
) -> str | None:
    """GET one page, using the cache and the global rate limiter.

    Returns the HTML string, or None if the page is missing or never loaded.
    Cache hits do not call ``limiter.acquire`` and do not touch the network.
    Retries back off on 403/429/5xx but still pass through the limiter, so a
    retry cannot sneak in ahead of the one-request-per-second rule.
    """
    sleep = sleeper or time.sleep
    cached, html = cache.lookup(url)
    if cached:
        logger.debug("cache hit %s", url)
        return html

    backoff = 2.0
    last_status = None
    for attempt in range(max_retries + 1):
        limiter.acquire()
        try:
            resp = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            logger.info("fetch error %s (%s); backing off %.0fs", url, exc, backoff)
            sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        last_status = resp.status_code
        if resp.status_code == 200 and "html" in resp.headers.get("content-type", "").lower():
            cache.store(url, 200, resp.text)
            return resp.text
        if resp.status_code == 404:
            cache.store(url, 404, None)
            return None
        if resp.status_code in (403, 429, 500, 502, 503, 504):
            retry_after = resp.headers.get("Retry-After")
            try:
                extra = float(retry_after) if retry_after else backoff
            except ValueError:
                extra = backoff
            extra = min(max(extra, backoff), 90)
            logger.info("HTTP %s on %s; backing off %.0fs (attempt %s)",
                        resp.status_code, url, extra, attempt + 1)
            sleep(extra)
            backoff = min(backoff * 2, 60)
            continue
        # Other 4xx: remember nothing (the URL may be a redirect oddity) and stop.
        logger.info("HTTP %s on %s; skipping", resp.status_code, url)
        return None

    logger.info("gave up on %s after retries (last status %s)", url, last_status)
    return None


# ---------------------------------------------------------------------------
# HTML → paragraphs, then keyword windows
# ---------------------------------------------------------------------------

def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def html_to_paragraphs(html_text: str) -> list[str]:
    """Visible text blocks from one wiki page, in reading order.

    Navigation, scripts, and the MediaWiki chrome are dropped. The content
    root is ``#mw-content-text`` (2008–2021) or ``<main>`` (newer wikis) when
    those exist. Block tags are preferred; if a team put the write-up in bare
    ``<div>``s, the text is split on blank lines instead.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        tag.decompose()
    for tag in soup.select("#toc, .toc, .mw-editsection, .navbar, .menu"):
        tag.decompose()

    root = (
        soup.select_one("#mw-content-text")
        or soup.select_one("main")
        or soup.select_one("article")
        or soup.body
        or soup
    )

    paragraphs: list[str] = []
    for el in root.find_all(_BLOCK_TAGS):
        # A <td> or <li> that itself contains a <p> would double-count.
        if el.find(_BLOCK_TAGS):
            continue
        text = _norm_space(el.get_text(" ", strip=True))
        if len(text) >= 25:
            paragraphs.append(text)

    if paragraphs:
        return paragraphs

    # Div-only pages: group non-empty lines into paragraphs, dropping the
    # short crumbs that make up a menu.
    chunks: list[str] = []
    buf: list[str] = []
    for raw_line in root.get_text("\n", strip=True).splitlines():
        line = _norm_space(raw_line)
        if not line:
            if buf:
                chunks.append(" ".join(buf))
                buf = []
            continue
        if len(line) < 40 and not buf:
            continue
        buf.append(line)
    if buf:
        chunks.append(" ".join(buf))
    return [c for c in chunks if len(c) >= 40]


def matched_terms(text: str) -> list[str]:
    """Labels of every failure pattern found in ``text`` (case-insensitive)."""
    return [label for label, pattern in _FAILURE_PATTERNS if pattern.search(text)]


@dataclass
class KeywordHit:
    """One keyword match plus the paragraph window around it."""

    paragraph_index: int
    matched_terms: list[str]
    passage: str


def find_keyword_hits(paragraphs: list[str], window: int = DEFAULT_WINDOW) -> list[KeywordHit]:
    """High-recall screen. Does not decide whether a hit is a real failure.

    ``window`` is how many paragraphs to keep on each side of the hit. The
    hit paragraph is always included. Overlapping windows are returned
    separately; later de-duplication collapses identical extracted cases.
    """
    hits: list[KeywordHit] = []
    seen_spans: set[tuple[int, int]] = set()
    for i, paragraph in enumerate(paragraphs):
        terms = matched_terms(paragraph)
        if not terms:
            continue
        start = max(0, i - window)
        end = min(len(paragraphs), i + window + 1)
        if (start, end) in seen_spans:
            continue
        seen_spans.add((start, end))
        passage = "\n\n".join(paragraphs[start:end])
        hits.append(KeywordHit(paragraph_index=i, matched_terms=terms, passage=passage))
    return hits


# ---------------------------------------------------------------------------
# Heuristic extractor (no API key required)
# ---------------------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT.split(_norm_space(text))
    return [p.strip() for p in parts if len(p.strip()) >= 15]


def _labels_in(sentence: str) -> list[str]:
    return matched_terms(sentence)


def _sentence_is_failure(sentence: str) -> bool:
    """True when this sentence itself reports an expression failure.

    The decision uses the sentence, not its neighbours, so a promoter-activity
    sentence does not become a failure just because the next paragraph is a
    Western blot.
    """
    labels = _labels_in(sentence)
    if not labels:
        return False

    inclusion = "inclusion body" in labels
    if _AVOIDANCE.search(sentence) and inclusion and not _HARD_FAILURE.search(sentence):
        return False

    strong = any(label in _STRONG_LABELS for label in labels)
    no_band = any(label in _NO_BAND_LABELS for label in labels)

    # A strong phrase ("expression failed", "inclusion bodies") is enough.
    # The stricter gel check applies only when "no band" is the only hit,
    # so a cloning gel cannot veto a sentence that also says expression failed.
    if no_band and not strong:
        # "No band at 800 bp, cassette removal successful" is a cloning gel.
        if not _PROTEIN_GEL.search(sentence):
            return False
        if _CLONING_GEL.search(sentence) and not re.search(
            r"\b(?:protein|sds|western|kda|soluble|insoluble)\b", sentence, re.I
        ):
            return False

    if strong:
        return _EXPERIMENT_CUE.search(sentence) is not None

    # Weak phrases ("not expressed", "no expression", "did not express") need
    # a protein-experiment word in the same sentence. A no-band line that
    # already passed the gel check does not need a second look.
    if no_band:
        return True
    return _PROTEIN_CONTEXT.search(sentence) is not None and _EXPERIMENT_CUE.search(sentence) is not None


def _tidy_host(raw: str) -> str:
    text = _norm_space(raw)
    text = re.sub(r"E\.?\s*coli", "E. coli", text, flags=re.I)
    text = re.sub(r"BL21\s*\(\s*DE3\s*\)", "BL21(DE3)", text, flags=re.I)
    text = re.sub(r"DH5\s*(?:α|alpha)", "DH5α", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .,;")
    return text


def extract_host(text: str) -> str | None:
    """Most specific host/strain mention in ``text``, or None."""
    for pattern in _HOST_RES:
        match = pattern.search(text)
        if not match:
            continue
        host = _tidy_host(match.group(0))
        # "BL21" alone is more useful as "E. coli BL21" when both are present.
        if host == "BL21" and re.search(r"E\.?\s*coli", text, re.I):
            return "E. coli BL21"
        if host.upper() == "WB800N":
            return "Bacillus subtilis WB800n" if re.search(r"Bacillus", text, re.I) else "WB800n"
        return host
    return None


def extract_conditions(text: str) -> str | None:
    """Induction, temperature, media, and OD bits, joined in reading order."""
    bits: list[str] = []

    for match in re.finditer(r"\b(LB|TB|2xYT|2×YT|M9|SOC|Terrific Broth)\b", text, re.I):
        bits.append(match.group(1).replace("×", "x").replace("2xYT", "2xYT"))

    iptg = re.compile(
        r"(\d+(?:\.\d+)?)\s*(mM|µM|uM|μM|nM|M)\s*IPTG"
        r"|IPTG(?:\s+concentration)?(?:\s+of|\s+at)?\s*(\d+(?:\.\d+)?)\s*(mM|µM|uM|μM|nM|M)",
        re.I,
    )
    for match in iptg.finditer(text):
        if match.group(1):
            bits.append(f"{match.group(1)} {match.group(2)} IPTG")
        else:
            bits.append(f"{match.group(3)} {match.group(4)} IPTG")

    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*°\s*C\b", text):
        bits.append(f"{match.group(1)}°C")

    for match in re.finditer(
        r"\bOD\s*600\s*(?:of|=|reached|at|to)?\s*(\d+(?:\.\d+)?)", text, re.I
    ):
        bits.append(f"OD600 {match.group(1)}")

    if re.search(r"induc|iptg", text, re.I):
        for match in re.finditer(
            r"\bfor\s+(\d+(?:\.\d+)?)\s*(hours|hrs|hr|h|minutes|mins|min)\b",
            text,
            re.I,
        ):
            bits.append(f"induction {match.group(1)} {match.group(2)}")

    unique: list[str] = []
    seen: set[str] = set()
    for bit in bits:
        key = bit.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(bit)
    return "; ".join(unique) or None


def extract_sequence(text: str) -> str | None:
    """BioBrick part IDs and, when the passage prints one, a raw sequence.

    Part IDs (``BBa_...``) come first. A long DNA or protein string is added
    only when it is actually written out in the passage. Sequences longer than
    500 characters are truncated so one JSONL line stays readable.
    """
    parts = []
    seen: set[str] = set()
    for match in _BBA.finditer(text):
        part = match.group(0)
        if part not in seen:
            seen.add(part)
            parts.append(part)

    raw: list[str] = []
    for match in _DNA.finditer(text):
        seq = match.group(0).upper().replace("U", "T")
        if len(seq) > 500:
            seq = seq[:500] + "…"
        raw.append(seq)
        break
    if not raw:
        for match in _AA.finditer(text):
            seq = match.group(0)
            if len(seq) > 500:
                seq = seq[:500] + "…"
            raw.append(seq)
            break

    pieces = parts + raw
    return "; ".join(pieces) if pieces else None


def failure_reason_for(sentence: str) -> str:
    """Short reason grounded in the sentence, not a guess about mechanism."""
    cause = re.search(
        r"\b(?:because|due to|caused by|as a result of)\s+([^.]{8,180})",
        sentence,
        re.I,
    )
    if cause:
        return _norm_space(cause.group(0)).rstrip(",;")
    if re.search(r"inclusion[\s\-]*bod", sentence, re.I):
        return "protein accumulated as inclusion bodies"
    if re.search(r"western", sentence, re.I) and re.search(r"negative", sentence, re.I):
        return "western blot negative"
    if re.search(r"\bno\s+(?:visible\s+|clear\s+|obvious\s+|distinct\s+)?bands?\b", sentence, re.I):
        return "no band detected on the protein gel"
    if re.search(r"\bbands?\s+(?:was|were|is|are)\s+not\b", sentence, re.I):
        return "expected band was not observed"
    if re.search(r"fail", sentence, re.I):
        return "expression failed"
    return "protein was not expressed"


def _paragraph_reports_failure(paragraph: str) -> bool:
    return any(_sentence_is_failure(sentence) for sentence in _split_sentences(paragraph))


def _field_from_paragraph(extract, paragraph: str, previous: str):
    """Read a field from this paragraph.

    The previous paragraph is used only when this one does not say, and only
    when that previous paragraph is not itself a different failure. Otherwise
    a Western blot in the paragraph above would lend its strain to the next,
    unrelated part.
    """
    found = extract(paragraph)
    if found:
        return found
    if previous and not _paragraph_reports_failure(previous):
        return extract(previous)
    return None


def heuristic_cases(passage: str) -> list[dict]:
    """Structured failure cases from one passage, using only regexes.

    The passage is the keyword window (paragraphs joined by a blank line).
    Each accepted sentence becomes one case. Host, conditions, and part IDs
    are taken from that sentence's own paragraph, which is where teams
    usually put the strain and the IPTG concentration next to the result.
    """
    paragraphs = [part.strip() for part in passage.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [passage.strip()] if passage.strip() else []

    cases: list[dict] = []
    for index, paragraph in enumerate(paragraphs):
        previous = paragraphs[index - 1] if index else ""
        for sentence in _split_sentences(paragraph):
            if not _sentence_is_failure(sentence):
                continue
            result = sentence.strip()
            if len(result) > 600:
                result = result[:600].rstrip() + "…"
            snippet = _norm_space(paragraph)
            if len(snippet) > 1200:
                snippet = snippet[:1200].rstrip() + "…"
            cases.append({
                "sequence": _field_from_paragraph(extract_sequence, paragraph, previous),
                "host": _field_from_paragraph(extract_host, paragraph, previous),
                "conditions": _field_from_paragraph(extract_conditions, paragraph, previous),
                "result": result,
                "failure_reason": failure_reason_for(sentence),
                "source_snippet": snippet,
            })
    return cases


# ---------------------------------------------------------------------------
# LLM extractor (OpenAI-compatible). Optional.
# ---------------------------------------------------------------------------

_LLM_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "is_expression_failure": {"type": "boolean"},
                    "sequence": {"type": ["string", "null"]},
                    "host": {"type": ["string", "null"]},
                    "conditions": {"type": ["string", "null"]},
                    "result": {"type": "string"},
                    "failure_reason": {"type": ["string", "null"]},
                    "source_quote": {"type": "string"},
                },
                "required": [
                    "is_expression_failure", "sequence", "host", "conditions",
                    "result", "failure_reason", "source_quote",
                ],
            },
        }
    },
    "required": ["cases"],
}

_LLM_SYSTEM = """You extract protein expression failure cases from iGEM team wiki text.

A case is a report that a protein was not expressed, was expressed only as inclusion bodies, or was not detected (no band on a protein gel, negative Western blot) under stated conditions.

Do not extract:
- plans or advice to avoid inclusion bodies
- successful cloning or PCR gels ("no band" because a cassette was removed)
- paper titles or bibliography lines
- promoter-activity tests that are not about producing a recombinant protein

Rules:
- Use only facts written in the passage. If a field is not stated, return null.
- sequence is a BioBrick id like BBa_K12345 and/or a raw DNA or protein sequence printed in the passage. Otherwise null.
- host is the expression strain or species (for example "E. coli BL21(DE3)").
- conditions is one string covering induction, temperature, IPTG concentration, medium, and time, if stated.
- result is one sentence describing what was observed.
- failure_reason is a short cause or observation (inclusion bodies, no band, western blot negative, or a because-clause from the text).
- source_quote must be copied from the passage, not paraphrased.
- If the passage has no expression failure, return {"cases": []}.
Return JSON matching the schema. Do not wrap it in markdown."""


class LLMError(RuntimeError):
    """The model call failed. Callers fall back to the heuristic extractor."""


def _nullish(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in {"", "null", "none", "n/a", "na", "unknown", "not stated"}:
            return None
        return text
    return str(value)


def _quote_in_passage(passage: str, quote: str) -> bool:
    def norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip().lower()
    return bool(quote) and norm(quote) in norm(passage)


def parse_llm_payload(payload: dict, passage: str) -> list[dict]:
    """Validate one model response against the passage it was shown.

    Cases with ``is_expression_failure`` false are dropped. A ``source_quote``
    that does not actually occur in the passage is replaced with the head of
    the passage, so a hallucinated quote cannot become the stored snippet.
    """
    if not isinstance(payload, dict):
        raise LLMError("model response was not a JSON object")
    raw_cases = payload.get("cases")
    if raw_cases is None:
        raise LLMError("model response had no 'cases' array")
    if not isinstance(raw_cases, list):
        raise LLMError("'cases' was not a list")

    cases: list[dict] = []
    for item in raw_cases:
        if not isinstance(item, dict):
            continue
        if item.get("is_expression_failure") is not True:
            continue
        result = _nullish(item.get("result"))
        if not result:
            continue
        quote = _nullish(item.get("source_quote")) or ""
        if _quote_in_passage(passage, quote):
            snippet = _norm_space(quote)
        else:
            snippet = _norm_space(passage)[:1200]
        if len(snippet) > 1200:
            snippet = snippet[:1200].rstrip() + "…"
        if len(result) > 600:
            result = result[:600].rstrip() + "…"
        cases.append({
            "sequence": _nullish(item.get("sequence")),
            "host": _nullish(item.get("host")),
            "conditions": _nullish(item.get("conditions")),
            "result": result,
            "failure_reason": _nullish(item.get("failure_reason")),
            "source_snippet": snippet,
        })
    return cases


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


class LLMExtractor:
    """Chat-completions client for any OpenAI-compatible server.

    ``post`` is injectable so tests can supply a fake HTTP call. A real call
    sends ``response_format: json_schema`` first. If the server rejects that
    (HTTP 400), it retries once with ``json_object``, which more local servers
    accept. The schema is also written into the prompt.
    """

    def __init__(self, api_key: str, base_url: str, model: str, *, post=None, timeout: int = 60):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self._post = post or requests.post

    @classmethod
    def from_env(cls, env: dict | None = None) -> LLMExtractor | None:
        """Build a client from the environment, or None when no key is set.

        Recognises ``IGEM_LLM_API_KEY`` / ``IGEM_LLM_BASE_URL`` /
        ``IGEM_LLM_MODEL``, and the ``OPENAI_*`` names as aliases. Placeholder
        values copied from ``.env.example`` (``your_...``) are treated as unset.
        """
        source = env if env is not None else __import__("os").environ
        key = (source.get("IGEM_LLM_API_KEY") or source.get("OPENAI_API_KEY") or "").strip()
        if not key or key.lower().startswith("your"):
            return None
        base = (
            source.get("IGEM_LLM_BASE_URL")
            or source.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).strip()
        model = (source.get("IGEM_LLM_MODEL") or source.get("OPENAI_MODEL") or "gpt-4o-mini").strip()
        return cls(api_key=key, base_url=base, model=model)

    def _request(self, response_format: dict, passage: str) -> dict:
        url = _chat_completions_url(self.base_url)
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": (
                    "Schema:\n" + json.dumps(_LLM_SCHEMA) + "\n\nPassage:\n" + passage
                )},
            ],
            "response_format": response_format,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._post(url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        return self._decode(resp)

    @staticmethod
    def _decode(resp) -> dict:
        status = getattr(resp, "status_code", 200)
        if status == 400:
            raise LLMError("schema response_format was rejected")
        if status >= 400:
            raise LLMError(f"LLM HTTP {status}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("LLM response was not JSON") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM response had no message content") from exc
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        if not isinstance(content, str):
            raise LLMError("LLM message content was not text")
        content = content.strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError("LLM content was not valid JSON") from exc
        return parsed

    def extract(self, passage: str) -> list[dict]:
        """Return zero or more cases. Raises ``LLMError`` on transport/parse failure.

        An empty list is a real answer ("nothing here is a failure"), not an
        error, and must not be replaced by the heuristic.
        """
        clipped = passage if len(passage) <= 6000 else passage[:6000]
        schema_format = {
            "type": "json_schema",
            "json_schema": {"name": "expression_failures", "schema": _LLM_SCHEMA},
        }
        try:
            payload = self._request(schema_format, clipped)
        except LLMError as exc:
            if "rejected" not in str(exc):
                raise
            logger.info("Server rejected json_schema; retrying with json_object.")
            payload = self._request({"type": "json_object"}, clipped)
        return parse_llm_payload(payload, clipped)


def extract_cases(passage: str, llm: LLMExtractor | None) -> tuple[list[dict], str]:
    """Normalise one passage. Returns ``(cases, method)`` with method ``llm`` or ``heuristic``."""
    if llm is not None:
        try:
            return llm.extract(passage), "llm"
        except LLMError as exc:
            logger.warning("LLM extraction failed (%s); using the heuristic.", exc)
    return heuristic_cases(passage), "heuristic"


# ---------------------------------------------------------------------------
# Crawl scope — same team boundary as igem_wiki.py, different page filter
# ---------------------------------------------------------------------------

def wiki_url_for(team_name: str, year: int) -> str:
    """Build a landing-page URL when the teams CSV has no ``wikiURL``.

    2022 and later use the static-wiki slug (lower case, hyphens). Earlier
    years use the MediaWiki ``Team:`` title. The CSV URL is more reliable
    when a team's slug does not match its display name; this is the fallback.
    """
    year = int(year)
    name = team_name.strip()
    if year >= 2022:
        slug = name.lower().replace("_", "-").replace(" ", "-")
        slug = re.sub(r"-{2,}", "-", slug).strip("-")
        return f"https://{year}.igem.wiki/{slug}"
    wiki_name = re.sub(r"\s+", "_", name)
    return f"https://{year}.igem.org/Team:{wiki_name}"


def page_in_scope(url: str, host: str, prefix: str) -> bool:
    """True if ``url`` is a content page inside this team's wiki.

    Host, path prefix, MediaWiki namespaces, and asset extensions follow
    ``igem_wiki._in_scope``. Query strings are rejected (robots.txt disallows
    ``/*?``). Notebook and Results pages are kept; sponsor/gallery/judging
    pages are not.
    """
    parsed = urlparse(url)
    if parsed.query:
        return False
    if parsed.netloc != host:
        return False
    path = parsed.path.rstrip("/")
    prefix_l = prefix.lower()
    path_l = path.lower()
    if not (path_l == prefix_l or path_l.startswith(prefix_l + "/")):
        return False
    if any(ns.lower() in path_l for ns in _SKIP_NAMESPACES):
        return False
    if path_l.endswith(tuple(ext.lower() for ext in _SKIP_EXTENSIONS)):
        return False
    tail = path_l[len(prefix_l):].strip("/")
    if not tail:
        return True
    if tail.split("/")[-1] in _SKIP_PAGE_EXACT:
        return False
    if any(piece in tail for piece in _SKIP_PAGE_SUBSTR):
        return False
    return True


def _page_priority(url: str) -> int:
    """Lower numbers are fetched sooner. The landing page is queued as -1."""
    path = urlparse(url).path.lower()
    if any(hint in path for hint in _PRIORITY_HINTS):
        return 0
    return 1


def crawl_team_pages(
    wiki_url: str,
    *,
    session: requests.Session,
    limiter: StrictRateLimiter,
    cache: PageCache,
    max_pages: int = 12,
    timeout: int = 30,
    max_retries: int = 3,
) -> list[tuple[str, str]]:
    """Fetch up to ``max_pages`` HTML pages inside one team's wiki.

    The landing page is first (that is where the sub-page links are). Later
    pages are ordered so Results, experiments, notebooks, and protein pages
    are read before the rest. Returns ``(canonical_url, html)`` pairs.
    Link discovery reuses ``derive_crawl_scope`` from ``igem_wiki.py``.
    """
    scope = derive_crawl_scope(wiki_url)
    if scope is None:
        logger.info("No crawl scope for %s", wiki_url)
        return []
    host, prefix = scope

    if "User-Agent" not in session.headers:
        session.headers["User-Agent"] = USER_AGENT

    start = _canonical(wiki_url.strip())
    # Heap entries are (priority, sequence, url). Sequence keeps the order stable.
    queue: list[tuple[int, int, str]] = [(-1, 0, start)]
    seen: set[str] = {start}
    pages: list[tuple[str, str]] = []
    seq = 1

    while queue and len(pages) < max_pages:
        queue.sort()
        _prio, _seq, url = queue.pop(0)
        html = fetch_html(
            session, url, limiter=limiter, cache=cache,
            timeout=timeout, max_retries=max_retries,
        )
        if not html:
            continue
        pages.append((url, html))
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            nxt = _canonical(urljoin(url, anchor["href"]))
            if nxt in seen:
                continue
            if not page_in_scope(nxt, host, prefix):
                continue
            seen.add(nxt)
            queue.append((_page_priority(nxt), seq, nxt))
            seq += 1

    return pages


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _as_year(year) -> int | str:
    try:
        return int(year)
    except (TypeError, ValueError):
        return year if year is not None else ""


def records_from_html(
    html_text: str,
    *,
    team_name: str,
    year,
    url: str,
    llm: LLMExtractor | None = None,
    window: int = DEFAULT_WINDOW,
) -> list[dict]:
    """Keyword-screen one page and return failure records (possibly empty)."""
    paragraphs = html_to_paragraphs(html_text)
    records: list[dict] = []
    for hit in find_keyword_hits(paragraphs, window=window):
        cases, method = extract_cases(hit.passage, llm)
        for case in cases:
            records.append({
                "team_name": team_name,
                "year": _as_year(year),
                "url": url,
                "sequence": case.get("sequence"),
                "host": case.get("host"),
                "conditions": case.get("conditions"),
                "result": case.get("result"),
                "failure_reason": case.get("failure_reason"),
                "source_snippet": case.get("source_snippet") or "",
                "extraction_method": method,
            })
    return dedupe_records(records)


def scrape_team(
    team: dict,
    *,
    session: requests.Session,
    limiter: StrictRateLimiter,
    cache: PageCache,
    llm: LLMExtractor | None = None,
    max_pages: int = 12,
    window: int = DEFAULT_WINDOW,
) -> list[dict]:
    """Crawl one team and return de-duplicated failure records."""
    pages = crawl_team_pages(
        team["wiki_url"],
        session=session,
        limiter=limiter,
        cache=cache,
        max_pages=max_pages,
    )
    records: list[dict] = []
    for url, html in pages:
        records.extend(records_from_html(
            html,
            team_name=team["team_name"],
            year=team["year"],
            url=url,
            llm=llm,
            window=window,
        ))
    return dedupe_records(records)


def record_key(record: dict) -> tuple:
    """Identity of a case: same team, page, part, and result sentence."""

    def norm(value) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip().lower()

    return (
        norm(record.get("team_name")),
        str(record.get("year")),
        norm(record.get("url")),
        norm(record.get("sequence")),
        norm(record.get("result"))[:240],
    )


def dedupe_records(records: list[dict]) -> list[dict]:
    """Drop later records that repeat an earlier case. Order is preserved."""
    kept: list[dict] = []
    seen: set[tuple] = set()
    for record in records:
        key = record_key(record)
        if key in seen:
            continue
        seen.add(key)
        kept.append(record)
    return kept


def load_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def write_jsonl(path: Path, records: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def merge_records(existing: list[dict], new: list[dict],
                  replace_teams: set[tuple[str, str]]) -> list[dict]:
    """Replace prior rows for the teams just scraped, then de-duplicate.

    A second run updates those teams in place and leaves every other team in
    the file alone.
    """
    def team_key(record: dict) -> tuple[str, str]:
        return (str(record.get("team_name") or ""), str(record.get("year") or ""))

    kept = [record for record in existing if team_key(record) not in replace_teams]
    return dedupe_records(kept + new)
