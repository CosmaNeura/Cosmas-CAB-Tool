"""Fail-closed text screening shared by wholly synthetic demo artifacts."""
from __future__ import annotations

import re
import unicodedata


# These patterns are intentionally conservative. A match is release-blocking
# even when a contributor believes the identifier-shaped value is fictional.
DIRECT_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    ),
    (
        "US phone number",
        re.compile(
            r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)"
        ),
    ),
    (
        "US Social Security number",
        re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    ),
    (
        "calendar date",
        re.compile(
            r"(?<!\d)(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])"
            r"[/-](?:19|20)\d{2}(?!\d)"
        ),
    ),
    (
        "ISO calendar date",
        re.compile(
            r"(?<!\d)(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-"
            r"(?:0[1-9]|[12]\d|3[01])(?!\d)"
        ),
    ),
    (
        "written calendar date",
        re.compile(
            r"\b(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+"
            r"(?:[1-9]|[12]\d|3[01])(?:,\s*(?:19|20)\d{2})?\b",
            re.I,
        ),
    ),
    (
        "web address",
        re.compile(
            r"(?:\bhttps?://|\bwww\.|\b(?:[A-Z0-9-]+\.)+"
            r"[A-Z]{2,63}\b)",
            re.I,
        ),
    ),
    (
        "IPv4 address",
        re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])"),
    ),
    (
        "IPv6 address",
        re.compile(
            r"(?<![A-F0-9:])(?:[A-F0-9]{1,4}:){2,7}[A-F0-9]{1,4}"
            r"(?![A-F0-9:])",
            re.I,
        ),
    ),
    (
        "IPv6 address",
        re.compile(
            r"(?<![A-F0-9:])(?=[A-F0-9:]*::)[A-F0-9:]{2,39}"
            r"(?![A-F0-9:])",
            re.I,
        ),
    ),
    (
        "US ZIP code",
        re.compile(r"(?<!\d)\d{5}(?:-\d{4})?(?!\d)"),
    ),
    (
        "street address",
        re.compile(
            r"\b\d{1,6}\s+(?:[A-Z0-9.'-]+\s+){0,5}"
            r"(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln|"
            r"Drive|Dr|Court|Ct|Way|Highway|Hwy)\b",
            re.I,
        ),
    ),
    (
        "UUID",
        re.compile(
            r"\b[0-9A-F]{8}-[0-9A-F]{4}-[1-5][0-9A-F]{3}-"
            r"[89AB][0-9A-F]{3}-[0-9A-F]{12}\b",
            re.I,
        ),
    ),
    (
        "long numeric identifier",
        re.compile(r"(?<!\d)\d{7,}(?!\d)"),
    ),
    (
        "long alphanumeric identifier",
        re.compile(
            r"\b(?=[A-Z0-9_-]{12,}\b)(?=[A-Z0-9_-]*[A-Z])"
            r"(?=[A-Z0-9_-]*\d)[A-Z0-9_-]+\b",
            re.I,
        ),
    ),
    (
        "record-number label",
        re.compile(
            r"\b(?:MRN|medical record number|patient name|date of birth|DOB|"
            r"street address|member (?:ID|number)|account (?:ID|number)|"
            r"claim (?:ID|number)|encounter (?:ID|number)|"
            r"patient (?:ID|number)|provider (?:ID|number))\b",
            re.I,
        ),
    ),
    (
        "personal-name phrase",
        re.compile(
            r"\b(?:patient(?:'s)? name is|named patient|"
            r"(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Z][a-z]+\s+[A-Z][a-z]+)\b"
        ),
    ),
)

# Deliberately conservative for a code-free artifact: any bounded 3-7
# character token with the ICD-10-CM category shape is rejected, with or
# without a decimal. This also rejects clinical look-alikes such as A1C/H1N1.
_ICD_CODE_TOKEN = re.compile(
    r"(?<![A-Z0-9])[A-Z][0-9][A-Z0-9]"
    r"(?:\.[A-Z0-9]{1,4}|[A-Z0-9]{0,4})(?![A-Z0-9])",
    re.I,
)
_PROCEDURE_CODE_TOKEN = re.compile(
    r"(?<![A-Z0-9])(?:\d{5}|\d{4}[A-Z]|[A-Z]\d{4})(?![A-Z0-9])",
    re.I,
)
_ANSWER_BEARING_LANGUAGE = re.compile(
    r"\b(?:ICD(?:-?10(?:-CM)?)?|CPT|HCPCS|E/?M|"
    r"must[_ -]?capture|must[_ -]?not[_ -]?capture|"
    r"expected[_ -]?(?:code|output|result)|billable\s+code)\b",
    re.I,
)
_BIDI_CLASSES = {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"}


def prohibited_text_shapes(
    text: str,
    *,
    reject_code_tokens: bool = False,
) -> tuple[str, ...]:
    """Return stable violation names without echoing source text."""
    violations: list[str] = []
    for name, pattern in DIRECT_IDENTIFIER_PATTERNS:
        if pattern.search(text):
            violations.append(name)

    for character in text:
        if character in {"\n", "\t"}:
            continue
        category = unicodedata.category(character)
        bidi = unicodedata.bidirectional(character)
        if category in {"Cc", "Cf"} or bidi in _BIDI_CLASSES:
            violations.append("Unicode control or bidirectional character")
            break

    if reject_code_tokens:
        if _ICD_CODE_TOKEN.search(text):
            violations.append("diagnosis code token")
        if _PROCEDURE_CODE_TOKEN.search(text):
            violations.append("procedure code token")
        if _ANSWER_BEARING_LANGUAGE.search(text):
            violations.append("answer-bearing coding language")

    return tuple(dict.fromkeys(violations))
