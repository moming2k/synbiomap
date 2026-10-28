# Pipeline scripts

Each script is a runnable step that imports logic from `src/`. They read and
write files under `data/`, `models/`, `outputs/`, and `website/assets/`, with
parameters in `config/settings.yaml`.

**You usually don't need to run these.** The processed datasets in
`data/processed/` are committed, so the notebooks, figures, and website all work
from a fresh clone. Run the scripts only to rebuild the datasets from raw
sources (needs API keys in `.env`).

The numbering grew over two generations of the project, so a few numbers repeat
(e.g. two `07_` and two `12_` scripts) because analysis steps and figure steps
were numbered independently. The stages below are the real order.

## Stage 1 — Ingest raw data  *(needs API keys)*
| Script | Does |
|--------|------|
| `01_ingest_papers.py` | Papers from OpenAlex (three-layer keyword strategy). |
| `02_ingest_patents.py` | Patents from the USPTO Open Data Portal (ODP). |
| `03_ingest_projects.py` | iGEM projects and parts. |
| `03b_fetch_parts.py` | Fetch iGEM parts from the Registry and match them to projects. |
| `03c_fetch_team_rosters.py` | Add team-member names to `projects.csv`. |
| `03d_scrape_wiki_dois.py` | Scrape each iGEM team wiki for the DOIs it cites. |
| `03e_scrape_expression_failures.py` | Scrape iGEM wikis for protein-expression failures (JSONL). |

## Stage 2 — Base embed / cluster / visualize
| Script | Does |
|--------|------|
| `04_embed.py` | Generate embeddings (model set in `config/settings.yaml`). |
| `05_cluster.py` | UMAP projection + HDBSCAN clustering. |
| `05b_label_clusters.py` | Label clusters with Claude Haiku; optionally sub-cluster. |
| `05c_plot.py` | Preview plots. |
| `06_visualize.py` | Export data files for the website. |
| `07_cluster_relatedness.py` | City-level cluster co-membership ("local relatedness") test. |
| `07_export_figures.py` | Export interactive figures for the website. |

## Stage 3 — Fine-tune SPECTER2  *(produces `models/specter2_synbio/`)*
| Script | Does |
|--------|------|
| `build_finetune_data.py` | Build fine-tuning training pairs from the citation graph. |
| `finetune_specter2.py` | Fine-tune the SPECTER2 adapter on synthetic-biology citations. |
| `eval_recall_at_k.py` | Cross-genre retrieval evaluation (recall@k). |

## Stage 4 — Tripartite corpus + analysis  *(the decisive study)*
| Script | Does |
|--------|------|
| `08_build_tripartite_corpus.py` | Build the three-type (projects/papers/patents) corpus. |
| `09_embed_finetuned.py` | Embed the corpus with the fine-tuned adapter. |
| `10_cluster_tripartite.py` | Cluster the corpus in the fine-tuned space. |
| `11_robustness_kcurve.py` | Clustering-resolution robustness for the decisive test. |
| `12_label_clusters.py` | Draft topic labels, then have a human verify them. |

## Stage 5 — Oldham patent robustness check
| Script | Does |
|--------|------|
| `13_build_oldham_papers.py` | Convert Oldham's synthetic-biology corpus to our schema. |
| `build_patents_from_oldham.py` | Build a shared-schema `patents.csv` from the Oldham patents. |
| `fetch_patent_abstracts.py` | Fetch abstract text for the Oldham patents. |
| `geocode_oldham_patents.py` | Put the Oldham patents on a map. |

## Stage 6 — Figures, tables, and manuscript assets
| Script | Does |
|--------|------|
| `12_world_map.py` | World-map figure. |
| `13_cc_heatmap.py` | Carbon-capture heatmap figure. |
| `14_fig_pipeline_concept.py` | Pipeline-concept schematic. |
| `15_fig_provenance.py` | Data-provenance figure. |
| `16_regression_tables.py` | Regression tables for the manuscript. |
| `17_centroid_knockdown_table.py` | Centroid-knockdown robustness table. |
| `viz_igem_3d.py`, `viz_projects_papers_3d.py` | Interactive 3D scatter plots. |
| `export_paper_assets.py` | Export figures, tables, and stats for the manuscript. |
| `export_zotero_bib.py` | Build `manuscript/references.bib` from the local Zotero library. |

## Support / one-off utilities
`geocode_igem_teams.py` (geocode teams) · `fix_igem_institutions.py` (clean
institution names) · `fill_part_paper_ids.py` (fill part→paper DOI links) ·
`merge_additional_papers.py` (fold extra papers into `papers.csv`) ·
`compare_geocoding_llms.py` (benchmark geocoding models).
