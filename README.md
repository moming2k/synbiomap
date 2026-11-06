# Patents, Papers, Parts & Planet

**Do student iGEM projects, academic papers, and patents in synthetic biology form semantically related local innovation trajectories at the city level?**

This repository holds the full research pipeline and documentation website for my Master's thesis. It studies synthetic biology innovation across three kinds of knowledge artifact: student projects (iGEM), academic publications (OpenAlex), and patents (USPTO). We ask whether, within a city, these three types work on related topics. The primary worked example is **carbon capture using cyanobacterial fermentation.**

Claims here are about *semantic relatedness and association*

## Project website

[**zer0juice.github.io/synbiomap**](https://zer0juice.github.io/synbiomap): the main public-facing home for the project (background, methods, results, the carbon-capture case study, and interactive explorers).

## What's in this repo

| Path | What it is |
|------|-----------|
| `src/` | Reusable Python modules — ingestion, embedding, clustering, geocoding, analysis. All pipeline logic lives here. |
| `scripts/` | Numbered, runnable pipeline steps that call `src/`. See [`scripts/README.md`](scripts/README.md) for the full ordered list. |
| `notebooks/` | Walkthroughs and the case study. See [`notebooks/README.md`](notebooks/README.md). |
| `data/processed/` | The normalized datasets (committed — see below). |
| `config/settings.yaml` | Every pipeline parameter in one place. |
| `models/specter2_synbio/` | The fine-tuned SPECTER2 adapter used for the tripartite analysis. |
| `website/` | The Quarto documentation site (the top-level deliverable). |
| `manuscript/` | The LaTeX manuscript (`main.tex`). |
| `outputs/` | Generated figures and the Beamer slide deck. |

## Two ways to use this repo

**A. Explore the results (no credentials needed).** The processed datasets in
`data/processed/` and the website's precomputed projection data in
`website/assets/data/` are committed. You can open the notebooks, read the CSVs,
or run the website straight after cloning, with no API keys required.

```bash
git clone https://github.com/Zer0Juice/synbiomap
cd synbiomap
pip install -r requirements.txt
quarto preview website        # browse the site locally
```

**B. Rebuild from raw sources.** Re-running ingestion and embedding needs API
credentials and is time-consuming. Raw downloads and the embedding cache are
*not* committed (too large / re-fetchable); everything needed to regenerate them
is in `scripts/`.

```bash
cp .env.example .env          # then fill in your API keys
# place the iGEM CSV exports under data/raw/ (see scripts/03_ingest_projects.py)
# then follow the ordered steps in scripts/README.md
```

## Repository structure

```
├── config/settings.yaml       # all pipeline parameters
├── src/                        # reusable modules (imported by scripts/)
│   ├── ingest/                 # openalex.py, odp.py, patentsview.py, igem.py,
│   │                           #   igem_wiki.py, igem_expression_failures.py,
│   │                           #   normalize.py
│   ├── embed/embeddings.py     # SPECTER2 embedding + batch caching
│   ├── cluster/cluster.py      # UMAP + HDBSCAN
│   ├── geo/geocode.py          # Nominatim / OpenCage geocoding (cached)
│   └── utils/                  # schema.py (shared columns), config.py
├── scripts/                    # numbered pipeline steps — see scripts/README.md
├── notebooks/                  # walkthroughs — see notebooks/README.md
│   ├── pipeline.ipynb          # orchestrator for the ingest→export steps
│   ├── 02_tripartite_city_analysis.py   # main analysis (marimo)
│   └── 03_carbon_capture.py    # case study (marimo)
├── data/
│   ├── raw/                    # downloaded inputs (not committed)
│   ├── processed/              # normalized CSVs (committed)
│   │   ├── papers.csv  patents.csv  projects.csv  parts.csv
│   │   ├── artifacts_tripartite*.csv   # the three-type corpus + clusters
│   │   └── ...                 # city-level tables, robustness curves
│   └── embeddings/             # embedding cache + projections (not committed)
├── models/specter2_synbio/     # fine-tuned SPECTER2 adapter + eval metrics
├── website/                    # Quarto site (GitHub Pages)
├── manuscript/                 # LaTeX manuscript (main.tex) + generated tables/figures
└── outputs/                    # figures/ and slides/ (Beamer deck)
```

## Data sources

| Source                                                                                    | What it provides                    | Access                                             |
| ----------------------------------------------------------------------------------------- | ----------------------------------- | -------------------------------------------------- |
| [OpenAlex](https://openalex.org)                                                          | Academic papers                     | Free REST API; optional key for higher rate limits |
| [USPTO Open Data Portal](https://data.uspto.gov) / [PatentsView](https://patentsview.org) | Patents                             | Free API (key required)                            |
| [iGEM Registry](https://igem.org)                                                         | Student projects and BioBrick parts | REST API + CSV export                              |

## Protein expression failures on iGEM wikis

`scripts/03e_scrape_expression_failures.py` reads iGEM team wikis and writes one JSONL row per protein-expression failure (inclusion bodies, a failed expression, a missing band on a protein gel, a negative Western blot). The crawl reuses the team-wiki rules in `src/ingest/igem_wiki.py`: same two URL eras (MediaWiki through 2021, `igem.wiki` from 2022), same “stay inside this team” boundary, and the same research User-Agent. Lab notebooks stay in scope here, because that is where teams write up failed inductions. Pages are cached under `data/raw/igem_expression_failures/` (gitignored) and the crawler never exceeds **1 request per second**.

Without an API key, a deterministic heuristic fills the fields. With a key, the same passages go to any OpenAI-compatible chat endpoint (`IGEM_LLM_API_KEY`, `IGEM_LLM_BASE_URL`, `IGEM_LLM_MODEL`; `OPENAI_*` names work as aliases). `--heuristic-only` forces the heuristic so a sample file does not depend on a model.

```bash
python scripts/03e_scrape_expression_failures.py \
    --year 2017 2021 \
    --team CCU_Taiwan --team Stockholm \
    --max-pages 8 \
    --heuristic-only \
    --out samples/expression_failures_sample.jsonl
```

`--year` / `--year-start` / `--year-end` choose seasons, `--team` is a case-insensitive substring (repeat it for several teams), `--max-pages` caps pages per team, and `--out` is the JSONL path. A small live sample is committed at `samples/expression_failures_sample.jsonl`. Each row has `team_name`, `year`, `url`, `sequence`, `host`, `conditions`, `result`, `failure_reason`, `source_snippet`, and `extraction_method` (`llm` or `heuristic`).

## Corpus construction strategy

**Papers** follow Shapira, Kwon & Youtie (2017, *Scientometrics*): a layered keyword approach where core terms (`"synthetic biology"`, `"synthetic genomics"`, `"BioBrick"`) are combined with subfield terms (`"repressilator"`, `"minimal genome"`, `"genetic toggle switch"`, …). Broad terms such as `"metabolic engineering"` are intentionally excluded — they would swamp the corpus with unrelated work. Retrieval is supplemented by citation expansion from two seed papers (see `config/settings.yaml → corpus`).

**Patents** data set is from Paul Oldham: https://github.com/poldham/synbio
This data is supplemented by abstracts and geocoding based on inventor addresses pulled through the [USPTO's Open Data Portal](https://data.uspto.gov)

All parameters live in `config/settings.yaml`.

## Methods summary

| Step | Method | Reference |
|------|--------|-----------|
| Corpus construction | Layered keyword + citation-seed search | Shapira et al. (2017); van Doren et al. (2013) |
| Case-study tagging | Keyword match on title + abstract (`carbon_capture_keywords`) | — |
| Embeddings | SPECTER2 (`allenai/specter2`), with a synthetic-biology fine-tuned adapter (`models/specter2_synbio/`) for the tripartite analysis | Cohan et al. (2020); Singh et al. (2022) |
| Dimensionality reduction | UMAP, cosine metric | McInnes et al. (2018) |
| Clustering | HDBSCAN | Campello et al. (2013) |
| Relatedness test | City-level cluster co-membership vs. a permutation null | `src/analyze/relatedness.py` |
| Geocoding | Nominatim (OpenStreetMap), cached | — |

Embeddings are cached as sharded `.npy` batches under `data/embeddings/`, not a single file.

## Reproducing the analysis

The full, ordered pipeline is documented in [`scripts/README.md`](scripts/README.md). In short: ingest (01–03) → base embed/cluster (04–07) → fine-tune SPECTER2 → build and analyze the tripartite corpus (08–12) → robustness and case-study figures/tables (13–17). The two marimo notebooks narrate the main analysis and the carbon-capture case study on top of the processed outputs.

## License

The **code** in this repository is released under the [MIT License](LICENSE) — Copyright (c) 2026 Zakhary Roth.

The **bundled datasets** are not covered by the MIT License; each retains the terms of its upstream source:

- **Papers** — [OpenAlex](https://openalex.org), released **CC0** (public domain).
- **Student projects and BioBrick parts** — [iGEM Registry](https://igem.org), **CC BY 4.0** (attribution).
- **Patents** — derived from Paul Oldham's synthetic-biology patent landscape dataset (*Synthetic Biology: Mapping the Patent Landscape*; [bioRxiv 10.1101/483826](https://doi.org/10.1101/483826), data at [OSF 10.17605/OSF.IO/73FMU](https://doi.org/10.17605/OSF.IO/73FMU)), licensed **CC BY-NC 3.0** — attribution required and **non-commercial use only**. Abstracts and inventor-address geocoding are supplemented from the [USPTO Open Data Portal](https://data.uspto.gov) (US public domain).

Because the patent data is NonCommercial, that portion of the datasets may only be reused non-commercially, even though the code is MIT-licensed. Please credit each source when reusing the data.