"""Text matching for the evals: tolerant of case, Arabic spelling variants and digit styles."""

import re
import unicodedata

# Arabic-Indic (٠-٩) and Eastern Arabic-Indic (۰-۹) digits -> 0-9.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
# Harakat (short vowels, tanween, shadda, sukun), superscript alef, tatweel.
_ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")
_ALEF_VARIANTS = re.compile(r"[أإآ]")
_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    # NFKC also turns Arabic presentation forms (as some PDF extractors return) into letters.
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_DIGITS)
    text = _ARABIC_MARKS.sub("", text)
    text = _ALEF_VARIANTS.sub("ا", text)
    text = _THOUSANDS_COMMA.sub("", text)
    return _SPACES.sub(" ", text.casefold()).strip()


def contains_any(text: str, alternatives: list[str]) -> bool:
    """True if `text` contains at least one of the alternatives, after normalizing both."""
    haystack = normalize(text)
    return any(normalize(alternative) in haystack for alternative in alternatives)
