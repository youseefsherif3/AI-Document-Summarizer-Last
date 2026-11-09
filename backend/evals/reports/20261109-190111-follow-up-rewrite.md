# Eval report: 20261109-190029-retrieval

- Mode: **retrieval** · retrieval: hybrid · top_k: 8 · min_similarity: 0.0
- Embedding model: `openai/text-embedding-3-small`
- Categories evaluated: follow_up (--category)
- Query rewrite: on, `nvidia/nemotron-3.5-lightning:free`
- Questions evaluated: **8 of 64**
- Questions sha: `7c7e5e1db02b6ae6` · corpus sha: `5b8ee69c49b101d9` · system prompt sha: `b55447865df8697e`
- Started 2026-11-09T19:00:29+00:00, finished 2026-11-09T19:01:11+00:00

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
| **All** | 8 | 50% | 75% | 75% | 0.604 |
| All except Arabic-PDF questions | 8 | 50% | 75% | 75% | 0.604 |
| lang: en | 8 | 50% | 75% | 75% | 0.604 |
| follow_up | 8 | 50% | 75% | 75% | 0.604 |

All expected sources in the top 8 (conflict + cross-document): –

## Rewritten questions (used for retrieval)

| ID | Question | Rewritten | First hit rank |
|---|---|---|---|
| fu-01 | And what about Brightwater? | And what about Brightwater employees' paid annual leave? | 2 |
| fu-03 | What are the company's core working hours? | What are Northwind Robotics' core working hours? | 1 |
| fu-04 | Who owns the second one? | Who owns RouteWise? | 1 |
| fu-05 | How quickly do incidents have to be reported there? | How quickly do incidents have to be reported to Northwind? | 1 |
| fu-06 | Explain the last one. | Explain the Restricted data class. | 1 |
| fu-07 | What do they expect for the next quarter? | What do they expect for Northwind's revenue in the next quarter after Q3 2026? | 3 |
| fu-08 | Is it the same at the other company? | Is it the same at the other company as at Brightwater? | not in top 8 |

## Top-1 similarity (for threshold tuning)

| Questions | Mean | Min | Max |
|---|---|---|---|
| answerable | 0.560 | 0.385 | 0.700 |

## Cost

| Part | Calls | Prompt tokens | Completion tokens | Cost (USD) |
|---|---|---|---|---|
| rewrite | 8 | 2071 | 96 | 0.000000 |
| embedding | 7 | 79 | 0 | 0.000002 |
| **Total** | | | | **0.000002** |

Estimated before the run: 0.000000 USD.

Questions sourced from the two Arabic PDFs are expected to fail (pypdf extraction is broken; see CLAUDE.md, Known issues).
