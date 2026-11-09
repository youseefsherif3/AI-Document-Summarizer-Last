# Eval report: 20261109-132740-retrieval

- Mode: **retrieval** · retrieval: vector · top_k: 8 · min_similarity: 0.0
- Embedding model: `openai/text-embedding-3-small`
- Questions sha: `0947b8d4a9cf25d6` · corpus sha: `5b8ee69c49b101d9` · system prompt sha: `b55447865df8697e`
- Started 2026-11-09T13:27:40+00:00, finished 2026-11-09T13:27:50+00:00

## Extraction quality (expected key facts found in the extracted text)

| Document | Format | Language | Facts found |
|---|---|---|---|
| `northwind_handbook.pdf` | pdf | en | 17/17 (100%) |
| `brightwater_policies.pdf` | pdf | en | 9/9 (100%) |
| `northwind_security_policy.docx` | docx | en | 7/7 (100%) |
| `brightwater_it_rules.docx` | docx | en | 4/4 (100%) |
| `northwind_q3_report.txt` | txt | en | 6/6 (100%) |
| `northwind_onboarding_memo.txt` | txt | en | 7/7 (100%) |
| `waha_library_ar.pdf` | pdf | ar | 9/14 (64%) |
| `nursin_port_ar.pdf` | pdf | ar | 2/6 (33%) |
| `nakheel_library_ar.txt` | txt | ar | 4/4 (100%) |

## Retrieval (answerable questions)

| Group | Questions | hit@1 | hit@3 | hit@8 | MRR |
|---|---|---|---|---|---|
| **All** | 49 | 53% | 86% | 94% | 0.684 |
| All except Arabic-PDF questions | 35 | 69% | 91% | 100% | 0.796 |
| lang: en | 38 | 61% | 84% | 95% | 0.719 |
| lang: ar | 11 | 27% | 91% | 91% | 0.561 |
| single_fact | 15 | 73% | 93% | 100% | 0.813 |
| two_page | 3 | 33% | 67% | 100% | 0.583 |
| table | 5 | 40% | 100% | 100% | 0.633 |
| conflict | 1 | 100% | 100% | 100% | 1.000 |
| newer_updates_older | 2 | 0% | 100% | 100% | 0.500 |
| arabic | 10 | 30% | 90% | 90% | 0.567 |
| english_on_arabic | 5 | 20% | 40% | 60% | 0.292 |
| cross_document | 5 | 80% | 80% | 100% | 0.850 |
| injection | 3 | 100% | 100% | 100% | 1.000 |

All expected sources in the top 8 (conflict + cross-document): 50%

## Top-1 similarity (for threshold tuning)

| Questions | Mean | Min | Max |
|---|---|---|---|
| answerable | 0.539 | 0.206 | 0.763 |
| unanswerable | 0.458 | 0.196 | 0.588 |

## Cost

| Part | Calls | Prompt tokens | Completion tokens | Cost (USD) |
|---|---|---|---|---|
| embedding | 10 | 3542 | 0 | 0.000071 |
| **Total** | | | | **0.000071** |

Estimated before the run: 0.000051 USD.

Questions sourced from the two Arabic PDFs are expected to fail (pypdf extraction is broken; see CLAUDE.md, Known issues).
