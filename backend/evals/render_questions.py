"""Write evals/questions.md, a readable view of questions.jsonl for review.

Run from backend/:  python -m evals.render_questions
"""

import json
from collections import Counter
from pathlib import Path

from .corpus_documents import DOCUMENTS

EVALS_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVALS_DIR / "questions.jsonl"
MARKDOWN_PATH = EVALS_DIR / "questions.md"

CATEGORIES = {
    "single_fact": "Single fact",
    "two_page": "Answer spans two pages",
    "table": "Table lookup",
    "conflict": "Conflicting values in two documents",
    "newer_updates_older": "Newer document updates an older one",
    "arabic": "Arabic question on an Arabic document",
    "english_on_arabic": "English question on an Arabic document",
    "cross_document": "Cross-document",
    "unanswerable": "Unanswerable (the model must say it can't find it)",
    "injection": "Prompt injection",
    "follow_up": "Follow-up question (only makes sense with the earlier conversation)",
}


def load_questions(path: Path = QUESTIONS_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def _sources(item: dict) -> str:
    if not item["sources"]:
        return "none (not in the corpus)"
    parts = []
    for source in item["sources"]:
        pages = source["pages"]
        where = "no page numbers" if pages is None else (
            f"page {pages[0]}" if len(pages) == 1 else f"pages {', '.join(map(str, pages))}"
        )
        parts.append(f"`{source['document']}` ({where})")
    return "; ".join(parts)


def _facts(item: dict) -> str:
    if not item["key_facts"]:
        return "none: the answer should say it is not in the documents"
    return "; ".join(" / ".join(f"`{alt}`" for alt in group) for group in item["key_facts"])


def render(questions: list[dict]) -> str:
    counts = Counter(item["category"] for item in questions)
    lines = [
        "# Eval questions (for review)",
        "",
        "Generated from `questions.jsonl` by `python -m evals.render_questions`. "
        "Do not edit by hand.",
        "",
        f"**{len(questions)} questions** in {len(counts)} categories. Key facts are matched "
        "case-insensitively, ignoring Arabic diacritics and digit style; alternatives for one "
        "fact are separated by `/`. The LLM judge (step 14) also scores each answer.",
        "",
        "## Corpus documents",
        "",
        "| File | Language | Pages | Description |",
        "|---|---|---|---|",
    ]
    for document in DOCUMENTS:
        pages = str(len(document.pages)) if document.numbered_pages else "none"
        lines.append(
            f"| `{document.filename}` | {document.language} | {pages} | {document.description} |"
        )

    lines += ["", "## Questions by category", ""]
    lines += [f"- {CATEGORIES[key]}: {counts.get(key, 0)}" for key in CATEGORIES]

    for key, title in CATEGORIES.items():
        items = [item for item in questions if item["category"] == key]
        lines += ["", f"## {title} ({len(items)})", ""]
        for item in items:
            lines.append(f"### {item['id']}: {item['question']}")
            lines.append("")
            for message in item.get("history", []):
                lines.append(f"- **Earlier, {message['role']}:** {message['content']}")
            lines.append(f"- **Expected key facts:** {_facts(item)}")
            lines.append(f"- **Expected source:** {_sources(item)}")
            if item["must_not_contain"]:
                forbidden = ", ".join(f"`{text}`" for text in item["must_not_contain"])
                lines.append(f"- **Must not contain (text match):** {forbidden}")
            for check in item["judge_checks"]:
                lines.append(f"- **Judge check:** {check}")
            if item["notes"]:
                lines.append(f"- **Notes:** {item['notes']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    questions = load_questions()
    MARKDOWN_PATH.write_text(render(questions), encoding="utf-8")
    print(f"Wrote {MARKDOWN_PATH.name}: {len(questions)} questions.")


if __name__ == "__main__":
    main()
