# Eval report: 20261109-183938-retrieval

- Mode: **retrieval** · retrieval: hybrid · top_k: 8 · min_similarity: 0.0
- Embedding model: `openai/text-embedding-3-small`
- Categories evaluated: follow_up (--category)
- Questions evaluated: **8 of 64**
- Questions sha: `7c7e5e1db02b6ae6` · corpus sha: `5b8ee69c49b101d9` · system prompt sha: `b55447865df8697e`
- Started 2026-11-09T18:39:38+00:00, finished 2026-11-09T18:39:40+00:00

## Extraction quality (expected key facts found in the extracted text)

| Document | Format | Language | Facts found |
|---|---|---|---|
| `northwind_handbook.pdf` | pdf | en | 3/3 (100%) |
| `brightwater_policies.pdf` | pdf | en | 2/2 (100%) |
| `northwind_security_policy.docx` | docx | en | 3/3 (100%) |
| `brightwater_it_rules.docx` | docx | en | 1/1 (100%) |
| `northwind_q3_report.txt` | txt | en | 2/2 (100%) |
| `northwind_onboarding_memo.txt` | txt | en | 0/0 (–) |
| `waha_library_ar.pdf` | pdf | ar | 0/0 (–) |
| `nursin_port_ar.pdf` | pdf | ar | 0/0 (–) |
| `nakheel_library_ar.txt` | txt | ar | 0/0 (–) |

## Retrieval (answerable questions)

| Group | Questions | hit@1 | hit@3 | hit@8 | MRR |
|---|---|---|---|---|---|
| **All** | 8 | 38% | 50% | 62% | 0.455 |
| All except Arabic-PDF questions | 8 | 38% | 50% | 62% | 0.455 |
| lang: en | 8 | 38% | 50% | 62% | 0.455 |
| follow_up | 8 | 38% | 50% | 62% | 0.455 |

All expected sources in the top 8 (conflict + cross-document): –

## Top-1 similarity (for threshold tuning)

| Questions | Mean | Min | Max |
|---|---|---|---|
| answerable | 0.368 | 0.157 | 0.507 |

## Cost

| Part | Calls | Prompt tokens | Completion tokens | Cost (USD) |
|---|---|---|---|---|
| embedding | 1 | 60 | 0 | 0.000001 |
| **Total** | | | | **0.000001** |

Estimated before the run: 0.000001 USD.

Questions sourced from the two Arabic PDFs are expected to fail (pypdf extraction is broken; see CLAUDE.md, Known issues).
