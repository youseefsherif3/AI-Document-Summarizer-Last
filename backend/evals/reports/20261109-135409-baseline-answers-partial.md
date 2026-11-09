# Eval report: 20261109-134740-answers (PARTIAL)

> **Partial report.** 2 of the 42 questions in this run were evaluated; the other 40 were not.
> **Why:** The free OpenRouter endpoints were congested, so the run was stopped on delivery day (2026-11-09) after 2 questions. With google/gemma-4-26b-a4b-it:free, the answer requests were refused with HTTP 429 (upstream rate limiting) 3-4 times per question before succeeding, about 3 minutes per question, and sf-03 was still being refused when the run was stopped. An earlier run with google/gemma-4-31b-it:free was refused the same way and stopped by itself after 1 question. The account's free-model daily limit was not reached (6 of 50 requests used), but the remaining 40 questions need 80 more requests, so finishing would also take two more days at 50 free requests per day. Run with EVAL_ANSWER_MODEL=google/gemma-4-26b-a4b-it:free set, so the settings match.
> **Not evaluated yet:** sf-03, sf-04, sf-05, sf-06, sf-07, sf-08, sf-09, sf-10, sf-11, sf-12, sf-13, sf-14, sf-15, tp-01, tp-02, tb-01, tb-02, tb-03, tb-04, tb-05, cf-01, up-01, up-02, ar-08, ar-09, ea-05, cd-01, cd-02, cd-03, cd-05, un-01, un-02, un-03, un-04, un-05, un-06, un-07, in-01, in-02, in-03
> **To finish the run:** `python -m evals.run_evals --mode answers --skip-arabic-pdf --resume 20261109-134740-answers` (from `backend/`; the progress file in `evals/runs/` is needed). All numbers below cover only the evaluated questions.

- Mode: **answers** · retrieval: vector · top_k: 8 · min_similarity: 0.0
- Embedding model: `openai/text-embedding-3-small`
- Answer model: `google/gemma-4-26b-a4b-it:free` · judge: `nvidia/nemotron-3-ultra-550b-a55b:free` (judge-v1). Answer scores describe this answer model, not the production chat model.
- Questions evaluated: **2 of 56** (14 excluded, listed below)
- Questions sha: `0947b8d4a9cf25d6` · corpus sha: `5b8ee69c49b101d9` · system prompt sha: `b55447865df8697e`
- Started 2026-11-09T13:47:40+00:00, finished 2026-11-09T13:54:09+00:00

## Excluded questions (14)

**Why:** Sourced from an Arabic PDF. pypdf extraction of the two Arabic PDFs is broken (CLAUDE.md, Known issues), so these questions are known to fail; they were left out with --skip-arabic-pdf. All other numbers in this report are for the 2 evaluated questions only.

| ID | Category | Question | Expected source(s) |
|---|---|---|---|
| tp-03 | two_page | ماذا يدفع العضو إذا فقد كتابًا مستعارًا من مكتبة الواحة؟ | `waha_library_ar.pdf` |
| ar-01 | arabic | متى تفتح مكتبة الواحة أبوابها؟ | `waha_library_ar.pdf` |
| ar-02 | arabic | في أي يوم تغلق مكتبة الواحة؟ | `waha_library_ar.pdf` |
| ar-03 | arabic | كم كتابًا يمكن للعضو استعارته من مكتبة الواحة، ولأي مدة؟ | `waha_library_ar.pdf` |
| ar-04 | arabic | كم تبلغ غرامة التأخير في مكتبة الواحة؟ | `waha_library_ar.pdf` |
| ar-05 | arabic | كم ساعة قبل الزيارة يجب حجز موعد في قاعة المخطوطات النادرة؟ | `waha_library_ar.pdf` |
| ar-06 | arabic | كم عدد قوارب الإرشاد في ميناء نورسين؟ | `nursin_port_ar.pdf` |
| ar-07 | arabic | ما هو الحد الأقصى للسرعة داخل ميناء نورسين؟ | `nursin_port_ar.pdf` |
| ar-10 | arabic | متى يُمنع التزود بالوقود في ميناء نورسين؟ | `nursin_port_ar.pdf` |
| ea-01 | english_on_arabic | What are the opening hours of the Waha Library? | `waha_library_ar.pdf` |
| ea-02 | english_on_arabic | How many pilot boats does Nursin port have? | `nursin_port_ar.pdf` |
| ea-03 | english_on_arabic | What is the pilotage fee at Nursin port for ships under 10,000 tons? | `nursin_port_ar.pdf` |
| ea-04 | english_on_arabic | Is photography allowed in the Waha Library's rare manuscripts room? | `waha_library_ar.pdf` |
| cd-04 | cross_document | Which library lets members borrow more books at once, the Waha Library or the Nakheel Library? | `waha_library_ar.pdf`, `nakheel_library_ar.txt` |

## Extraction quality (expected key facts found in the extracted text)

| Document | Format | Language | Facts found |
|---|---|---|---|
| `northwind_handbook.pdf` | pdf | en | 17/17 (100%) |
| `brightwater_policies.pdf` | pdf | en | 9/9 (100%) |
| `northwind_security_policy.docx` | docx | en | 7/7 (100%) |
| `brightwater_it_rules.docx` | docx | en | 4/4 (100%) |
| `northwind_q3_report.txt` | txt | en | 6/6 (100%) |
| `northwind_onboarding_memo.txt` | txt | en | 7/7 (100%) |
| `waha_library_ar.pdf` | pdf | ar | 0/0 (–) |
| `nursin_port_ar.pdf` | pdf | ar | 0/0 (–) |
| `nakheel_library_ar.txt` | txt | ar | 4/4 (100%) |

## Retrieval (answerable questions)

| Group | Questions | hit@1 | hit@3 | hit@8 | MRR |
|---|---|---|---|---|---|
| **All** | 2 | 100% | 100% | 100% | 1.000 |
| All except Arabic-PDF questions | 2 | 100% | 100% | 100% | 1.000 |
| lang: en | 2 | 100% | 100% | 100% | 1.000 |
| single_fact | 2 | 100% | 100% | 100% | 1.000 |

All expected sources in the top 8 (conflict + cross-document): –

## Top-1 similarity (for threshold tuning)

| Questions | Mean | Min | Max |
|---|---|---|---|
| answerable | 0.656 | 0.638 | 0.674 |

## Answers

| Group | Qs | Judge (1–5) | Key facts | Refusal acc. | False refusals | Citation correct | Citation coverage | Judge checks | Forbidden text | Judge errors |
|---|---|---|---|---|---|---|---|---|---|---|
| **All** | 2 | 5 | 100% | – | 0% | 100% | 100% | – | 0 | 0 |
| All except Arabic-PDF questions | 2 | 5 | 100% | – | 0% | 100% | 100% | – | 0 | 0 |
| single_fact | 2 | 5 | 100% | – | 0% | 100% | 100% | – | 0 | 0 |

## Cost

| Part | Calls | Prompt tokens | Completion tokens | Cost (USD) |
|---|---|---|---|---|
| chat | 9 | 2596 | 43 | 0.000000 (+7 unknown) |
| judge | 2 | 696 | 224 | 0.000000 |
| **Total** | | | | **0.000000** |

Estimated before the run: 0.000000 USD.

Questions sourced from the two Arabic PDFs are expected to fail (pypdf extraction is broken; see CLAUDE.md, Known issues).
