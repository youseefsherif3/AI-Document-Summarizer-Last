# Eval report: 20261109-192020-retrieval

- Mode: **retrieval** · retrieval: hybrid · top_k: 8 · min_similarity: 0.0
- Embedding model: `openai/text-embedding-3-small`
- Categories evaluated: conflict (--category)
- Query rewrite: off
- Questions evaluated: **3 of 66**
- Questions sha: `e6f9b784e8d46f4d` · corpus sha: `7ef608185dc75d86` · system prompt sha: `b55447865df8697e`
- Started 2026-11-09T19:20:20+00:00, finished 2026-11-09T19:20:23+00:00

## Extraction quality (expected key facts found in the extracted text)

| Document | Format | Language | Facts found |
|---|---|---|---|
| `northwind_handbook.pdf` | pdf | en | 2/2 (100%) |
| `brightwater_policies.pdf` | pdf | en | 1/1 (100%) |
| `northwind_security_policy.docx` | docx | en | 1/1 (100%) |
| `brightwater_it_rules.docx` | docx | en | 1/1 (100%) |
| `northwind_q3_report.txt` | txt | en | 1/1 (100%) |
| `northwind_onboarding_memo.txt` | txt | en | 0/0 (–) |
| `waha_library_ar.pdf` | pdf | ar | 0/0 (–) |
| `nursin_port_ar.pdf` | pdf | ar | 0/0 (–) |
| `nakheel_library_ar.txt` | txt | ar | 0/0 (–) |

## Retrieval (answerable questions)

| Group | Questions | hit@1 | hit@3 | hit@8 | MRR |
|---|---|---|---|---|---|
| **All** | 3 | 67% | 100% | 100% | 0.778 |
| All except Arabic-PDF questions | 3 | 67% | 100% | 100% | 0.778 |
| lang: en | 3 | 67% | 100% | 100% | 0.778 |
| conflict | 3 | 67% | 100% | 100% | 0.778 |

All expected sources in the top 8 (conflict + cross-document): 100%

## Top-1 similarity (for threshold tuning)

| Questions | Mean | Min | Max |
|---|---|---|---|
| answerable | 0.505 | 0.422 | 0.666 |

## Cost

| Part | Calls | Prompt tokens | Completion tokens | Cost (USD) |
|---|---|---|---|---|
| embedding | 4 | 363 | 0 | 0.000007 |
| **Total** | | | | **0.000007** |

Estimated before the run: 0.000008 USD.

Questions sourced from the two Arabic PDFs are expected to fail (pypdf extraction is broken; see CLAUDE.md, Known issues).
