"""Verified public terminology acceleration; any miss uses the existing builder.

The compressed payload hash is reviewed source authority, not a hash supplied by
the data file. No normalization or clinical decision is performed here.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from functools import lru_cache
from pathlib import Path
import re
import zlib

from . import diagnosis_terms

FORMAT = "cab-icd-terminology-index.v1"
GENERATOR_VERSION = "icd-index-generator.v1"
GENERATOR_SHA256 = "c57aa4d0d60cf6428587aa277869f2e5658882966232f338a7b688e586889d94"
CATALOG_SHA256 = "54bb7f4b56846e32f1fba7dfb3aa238017983f9ae3a3650e0cacbd5f563849ef"
TERMS_SHA256 = "156898a3eb0cb44e536b6ad4977ed9355a4a5fcce25e00369bbbfcfed61b4d15"
TERMS_VERSION = "clinical-terms.2026-10-02.v3"
CACHE_SHA256 = "3d22f133d04086f4e8faea87d961d22d00892bc4627eda02948b1ff4b83c89ca"
DATA = Path(__file__).resolve().parent.parent / "data"
CATALOG = DATA / "icd10cm_fy_current.csv.gz"
CACHE = DATA / "icd10_terminology_index.json.gz"
MAX_COMPRESSED = 4 * 1024 * 1024
MAX_DECOMPRESSED = 24 * 1024 * 1024
MAX_ENTRIES = 100_000
MAX_TERMS = 128
MAX_TOKEN = 128
_CODE = re.compile(r"[A-Z][A-Z0-9]{2,6}\Z")
_POSITIVE = re.compile(r"[a-z0-9_]+\Z")
_NEGATIVE = re.compile(r"absent:[a-z0-9_]+\Z")


def _read(path: Path, bound: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(bound + 1)
    if len(value) > bound:
        raise ValueError("index input too large")
    return value


def entries_sha256(entries: tuple[tuple[str, str], ...]) -> str:
    """Hash the effective current catalog in its original insertion order."""
    digest = hashlib.sha256(b"[")
    for position, (raw, description) in enumerate(entries):
        if position:
            digest.update(b",")
        digest.update(json.dumps([raw, description], ensure_ascii=True,
                                 separators=(",", ":")).encode("ascii"))
    digest.update(b"]")
    return digest.hexdigest()


def _object(pairs):
    # Only the small envelope is an object; all index rows are arrays.
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate index metadata")
    return result


def decode_index(blob: bytes, entries: tuple[tuple[str, str], ...]) -> dict:
    """Decode bounded, source-bound data. The caller must first verify its hash."""
    if len(blob) > MAX_COMPRESSED or not 0 < len(entries) <= MAX_ENTRIES:
        raise ValueError("index bound")
    with gzip.GzipFile(fileobj=io.BytesIO(blob), mode="rb") as stream:
        body = stream.read(MAX_DECOMPRESSED + 1)
    if len(body) > MAX_DECOMPRESSED:
        raise ValueError("decompressed index bound")
    document = json.loads(body, object_pairs_hook=_object)
    required = {"format", "generatorVersion", "generatorSha256", "catalogSha256",
                "termsSourceSha256", "termsVersion", "entriesSha256", "entryCount", "rows"}
    if type(document) is not dict or set(document) != required:
        raise ValueError("index schema")
    expected = {"format": FORMAT, "generatorVersion": GENERATOR_VERSION,
                "generatorSha256": GENERATOR_SHA256, "catalogSha256": CATALOG_SHA256,
                "termsSourceSha256": TERMS_SHA256, "termsVersion": TERMS_VERSION,
                "entriesSha256": entries_sha256(entries)}
    if any(document[key] != value for key, value in expected.items()):
        raise ValueError("index source mismatch")
    if type(document["entryCount"]) is not int or document["entryCount"] != len(entries):
        raise ValueError("index count")
    rows = document["rows"]
    if type(rows) is not list or not 0 < len(rows) <= len(entries):
        raise ValueError("index rows")
    positions = {raw: position for position, (raw, _) in enumerate(entries)}
    if len(positions) != len(entries) or any(not _CODE.fullmatch(raw) for raw in positions):
        raise ValueError("current code identity")
    index, seen = {}, set()
    previous_first = -1
    for row in rows:
        if type(row) is not list or len(row) != 3:
            raise ValueError("index row")
        positive, negative, raws = row
        for tokens, pattern in ((positive, _POSITIVE), (negative, _NEGATIVE)):
            if (type(tokens) is not list or len(tokens) > MAX_TERMS
                    or any(type(token) is not str or len(token) > MAX_TOKEN
                           or not pattern.fullmatch(token) for token in tokens)):
                raise ValueError("index terms")
        if negative != sorted(set(negative)):
            raise ValueError("index negative set")
        if type(raws) is not list or not 0 < len(raws) <= len(entries):
            raise ValueError("index code list")
        previous = -1
        for raw in raws:
            if type(raw) is not str or raw not in positions or raw in seen:
                raise ValueError("index code coverage")
            position = positions[raw]
            if position <= previous:
                raise ValueError("index ambiguity order")
            seen.add(raw)
            previous = position
        if positions[raws[0]] <= previous_first:
            raise ValueError("index key order")
        previous_first = positions[raws[0]]
        key = (tuple(positive), frozenset(negative))
        if key in index:
            raise ValueError("duplicate index key")
        index[key] = tuple(raws)
    if seen != set(positions):
        raise ValueError("incomplete index")
    return index


@lru_cache(maxsize=4)
def _cached_decode(blob: bytes, entries: tuple[tuple[str, str], ...], decoder_binding: tuple) -> dict:
    # Like _description_index, this private public-data index is shared by
    # readers, never mutated. Binding includes decoder metadata and bounds so
    # a changed validation contract cannot reuse an earlier acceptance.
    return decode_index(blob, entries)


def load_index(entries: tuple[tuple[str, str], ...], current_path: Path) -> dict | None:
    """Use only the exact bundled source. Never write or repair a runtime cache."""
    try:
        if (CACHE_SHA256 is None or current_path.resolve() != CATALOG.resolve()
                or diagnosis_terms.VERSION != TERMS_VERSION):
            return None
        if hashlib.sha256(_read(CATALOG, MAX_COMPRESSED)).hexdigest() != CATALOG_SHA256:
            return None
        terms = Path(diagnosis_terms.__file__)
        if hashlib.sha256(_read(terms, 128 * 1024)).hexdigest() != TERMS_SHA256:
            return None
        blob = _read(CACHE, MAX_COMPRESSED)
        if hashlib.sha256(blob).hexdigest() != CACHE_SHA256:
            return None
        binding = (FORMAT, GENERATOR_VERSION, GENERATOR_SHA256, CATALOG_SHA256,
                   TERMS_SHA256, TERMS_VERSION, MAX_COMPRESSED, MAX_DECOMPRESSED,
                   MAX_ENTRIES, MAX_TERMS, MAX_TOKEN)
        return _cached_decode(blob, entries, binding)
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError,
            RecursionError, EOFError, zlib.error):
        return None
