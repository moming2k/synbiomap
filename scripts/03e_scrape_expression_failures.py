"""
Step 3e — Scrape iGEM team wikis for protein-expression failures.

What this produces
------------------
A JSONL file with one record per failure case: a team reported that a protein
did not express, formed inclusion bodies, or gave no band / a negative Western
blot. See src/ingest/igem_expression_failures.py for the crawl rules, the
keyword list, and the LLM / heuristic split.

Where team URLs come from
-------------------------
Same place as scripts/03d_scrape_wiki_dois.py when the raw teams CSV is on
disk (data/raw/projects/igem_teams_with_descriptions_2004_2025.csv). That file
is not in git. If it is missing, the script uses the distinct team wiki URLs
already committed in data/processed/igem_wiki_dois.csv. A --team name that
matches nothing in either file is given a landing-page URL from the year and
the MediaWiki / igem.wiki pattern.

Politeness
----------
At most one HTTP request per second, shared across the whole run. Pages are
cached under data/raw/igem_expression_failures/pages/ (gitignored), so a rerun
does not fetch them again. Use --refresh-cache to ignore that cache.

The extractor
-------------
If IGEM_LLM_API_KEY (or OPENAI_API_KEY) is set, passages are sent to an
OpenAI-compatible chat-completions endpoint. Otherwise a deterministic
heuristic runs. --heuristic-only forces the heuristic even when a key is set,
which is the reproducible choice for a sample file.

Usage
-----
    # A few named teams in two years (a smoke run):
    python scripts/03e_scrape_expression_failures.py \\
        --year 2017 2021 \\
        --team CCU_Taiwan --team Stockholm \\
        --max-pages 8 \\
        --heuristic-only \\
        --out samples/expression_failures_sample.jsonl

    # One year range, first 5 teams, heuristic only:
    python scripts/03e_scrape_expression_failures.py \\
        --year-start 2019 --year-end 2019 --max-teams 5 --heuristic-only

    # With a local OpenAI-compatible server:
    #   IGEM_LLM_BASE_URL=http://localhost:8000/v1
    #   IGEM_LLM_MODEL=your-model
    #   IGEM_LLM_API_KEY=...
    python scripts/03e_scrape_expression_failures.py --year 2021 --team CCU_Taiwan
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.ingest.igem_expression_failures import (  # noqa: E402
    DEFAULT_MIN_INTERVAL,
    LLMExtractor,
    PageCache,
    StrictRateLimiter,
    USER_AGENT,
    load_jsonl,
    merge_records,
    scrape_team,
    wiki_url_for,
    write_jsonl,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

RAW_TEAMS = REPO_ROOT / "data" / "raw" / "projects" / "igem_teams_with_descriptions_2004_2025.csv"
WIKI_DOIS = REPO_ROOT / "data" / "processed" / "igem_wiki_dois.csv"
DEFAULT_CACHE = REPO_ROOT / "data" / "raw" / "igem_expression_failures" / "pages"
DEFAULT_OUT = REPO_ROOT / "data" / "processed" / "igem_expression_failures.jsonl"


def _load_dotenv() -> None:
    """Load repo .env into the process if python-dotenv is installed."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _unique_teams(rows: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for row in rows:
        wiki = (row.get("wiki_url") or "").strip()
        name = (row.get("team_name") or "").strip()
        year = str(row.get("year") or "").strip()
        if not wiki or not name or not year:
            continue
        key = (year, name.lower(), wiki.rstrip("/"))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "team_id": str(row.get("team_id") or ""),
            "team_name": name,
            "year": year,
            "wiki_url": wiki,
        })
    return out


def load_catalog() -> list[dict]:
    """Team landing pages, preferring the raw iGEM CSV and falling back to wiki DOIs."""
    if RAW_TEAMS.exists():
        logger.info("Team list: %s", _rel(RAW_TEAMS))
        with RAW_TEAMS.open(encoding="utf-8", newline="") as handle:
            rows = []
            for row in csv.DictReader(handle):
                rows.append({
                    "team_id": row.get("id") or row.get("team_id") or "",
                    "team_name": row.get("name") or row.get("team_name") or "",
                    "year": row.get("year") or "",
                    "wiki_url": row.get("wikiURL") or row.get("wiki_url") or "",
                })
        return _unique_teams(rows)

    if WIKI_DOIS.exists():
        logger.info("Raw teams CSV not found; using wiki URLs in %s", _rel(WIKI_DOIS))
        with WIKI_DOIS.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return _unique_teams(rows)

    logger.warning("No team catalog on disk. Only --team plus a year can be crawled.")
    return []


def _year_set(args) -> set[int] | None:
    years: set[int] = set(args.year or [])
    if args.year_start is not None or args.year_end is not None:
        start = args.year_start if args.year_start is not None else 2008
        end = args.year_end if args.year_end is not None else 2025
        if end < start:
            raise SystemExit("--year-end is earlier than --year-start.")
        years |= set(range(start, end + 1))
    return years or None


def select_teams(catalog: list[dict], args) -> list[dict]:
    """Apply the year range and the team-name filter.

    ``--team`` is a case-insensitive substring. A name that matches no catalog
    row is still crawled: the landing URL is built from the year and the team
    name (see ``wiki_url_for``). That is how a team missing from the DOI table
    can be included in a small run.
    """
    years = _year_set(args)
    filters = [name.strip().lower() for name in (args.team or []) if name.strip()]

    selected = []
    for team in catalog:
        try:
            year = int(team["year"])
        except (TypeError, ValueError):
            continue
        if years is not None and year not in years:
            continue
        if filters and not any(piece in team["team_name"].lower() for piece in filters):
            continue
        selected.append(team)

    matched_filters: set[str] = set()
    for team in selected:
        for piece in filters:
            if piece in team["team_name"].lower():
                matched_filters.add(piece)

    # Only invent URLs for names the catalog does not know in this year range.
    if filters and years is not None:
        for piece in filters:
            if piece in matched_filters:
                continue
            # Use the user's spelling (not the lower-cased filter) when we can.
            spelling = next(name for name in args.team if name.strip().lower() == piece)
            for year in sorted(years):
                selected.append({
                    "team_id": "",
                    "team_name": spelling.strip(),
                    "year": str(year),
                    "wiki_url": wiki_url_for(spelling.strip(), year),
                })

    selected.sort(key=lambda team: (int(team["year"]), team["team_name"].lower()))
    if args.max_teams:
        selected = selected[: args.max_teams]
    return selected


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape iGEM team wikis for protein-expression failure cases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See the module docstring in src/ingest/igem_expression_failures.py.",
    )
    parser.add_argument("--year", type=int, nargs="+",
                        help="Competition year or years, e.g. --year 2017 2021.")
    parser.add_argument("--year-start", type=int,
                        help="First year of an inclusive range. Combine with --year-end.")
    parser.add_argument("--year-end", type=int,
                        help="Last year of an inclusive range.")
    parser.add_argument("--team", action="append", default=[],
                        help="Team-name filter (substring, case-insensitive). Repeatable.")
    parser.add_argument("--max-teams", type=int,
                        help="Crawl at most this many teams after filtering.")
    parser.add_argument("--max-pages", type=int, default=12,
                        help="Max content pages to read per team (default 12). "
                             "Cached pages count toward the cap.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"JSONL output (default {_rel(DEFAULT_OUT)}).")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE,
                        help="Where fetched HTML is stored (default under data/raw/).")
    parser.add_argument("--rate", type=float, default=1.0,
                        help="Maximum requests per second. Must be in (0, 1]. Default 1.")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Ignore cached HTML and request pages again.")
    parser.add_argument("--heuristic-only", action="store_true",
                        help="Do not call an LLM, even if an API key is set.")
    parser.add_argument("--timeout", type=int, default=30,
                        help="Per-request timeout in seconds (default 30).")
    return parser.parse_args()


def run():
    args = parse_args()
    _load_dotenv()

    if args.rate <= 0 or args.rate > 1:
        raise SystemExit(
            "--rate must be in (0, 1]. This scraper never exceeds 1 request per second."
        )
    if not args.year and args.year_start is None and args.year_end is None \
            and not args.team and not args.max_teams:
        raise SystemExit(
            "Refusing to crawl every iGEM team. Pass --year / --year-start, "
            "--team, or --max-teams."
        )
    if args.max_pages < 1:
        raise SystemExit("--max-pages must be at least 1.")

    catalog = load_catalog()
    teams = select_teams(catalog, args)
    print(f"Teams to crawl: {len(teams)}")
    if not teams:
        print("Nothing matched. Check --year and --team.")
        return

    for team in teams:
        print(f"  {team['year']}  {team['team_name']}  {team['wiki_url']}")

    if args.refresh_cache and args.cache_dir.exists():
        removed = 0
        for path in args.cache_dir.glob("*"):
            if path.is_file():
                path.unlink()
                removed += 1
        print(f"Cleared {removed} cached files from {_rel(args.cache_dir)}")

    llm = None if args.heuristic_only else LLMExtractor.from_env()
    if llm is None:
        print("Extractor: heuristic (no LLM key, or --heuristic-only).")
    else:
        print(f"Extractor: LLM model {llm.model} at {llm.base_url}")

    interval = max(DEFAULT_MIN_INTERVAL, 1.0 / args.rate)
    # args.rate <= 1, so 1/rate >= 1. max() keeps the floor obvious.
    limiter = StrictRateLimiter(min_interval=interval)
    cache = PageCache(args.cache_dir)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})

    new_records = []
    started = time.monotonic()
    try:
        for index, team in enumerate(teams, start=1):
            print(f"\n[{index}/{len(teams)}] {team['team_name']} ({team['year']})", flush=True)
            records = scrape_team(
                team,
                session=session,
                limiter=limiter,
                cache=cache,
                llm=llm,
                max_pages=args.max_pages,
            )
            print(f"  {len(records)} failure case(s)")
            new_records.extend(records)
    finally:
        session.close()

    replace = {(team["team_name"], str(team["year"])) for team in teams}
    combined = merge_records(load_jsonl(args.out), new_records, replace)
    write_jsonl(args.out, combined)

    elapsed = time.monotonic() - started
    print(f"\nWrote {len(combined)} record(s) -> {_rel(args.out.resolve())}")
    print(f"New cases this run: {len(new_records)} in {elapsed:.0f}s "
          f"(cap {args.rate:g} req/s; cached pages are free).")
    for record in new_records[:3]:
        print(f"  - {record['team_name']} {record['year']}: {record['failure_reason']} "
              f"[{record['extraction_method']}]")


if __name__ == "__main__":
    run()
