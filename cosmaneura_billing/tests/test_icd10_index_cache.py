"""Public generated index must preserve meanings and fail back on every miss."""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path

import pytest

from cosmaneura_billing.knowledge import diagnosis_terms, icd10_ccsr
from cosmaneura_billing.knowledge import icd10_index_cache as cache
from cosmaneura_billing.settings import Settings


ENTRIES = (("A001", "Pain right hand"), ("A002", "Migraine without aura"),
           ("A003", "Pain right hand"), ("A004", "Diabetes due to condition"))


def _gzip(body):
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as stream:
        stream.write(body)
    return output.getvalue()


@pytest.fixture
def small_cache(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog.csv.gz"
    catalog.write_bytes(b"fixed fictional catalog input")
    path = tmp_path / "index.json.gz"
    monkeypatch.setattr(cache, "CATALOG", catalog)
    monkeypatch.setattr(cache, "CATALOG_SHA256", hashlib.sha256(catalog.read_bytes()).hexdigest())
    monkeypatch.setattr(cache, "CACHE", path)
    expected = icd10_ccsr._description_index(ENTRIES)
    document = {"format": cache.FORMAT, "generatorVersion": cache.GENERATOR_VERSION,
                "generatorSha256": cache.GENERATOR_SHA256, "catalogSha256": cache.CATALOG_SHA256,
                "termsSourceSha256": cache.TERMS_SHA256, "termsVersion": cache.TERMS_VERSION,
                "entriesSha256": cache.entries_sha256(ENTRIES), "entryCount": len(ENTRIES),
                "rows": [[list(pos), sorted(neg), list(raws)] for (pos, neg), raws in expected.items()]}

    def install(value):
        blob = _gzip(json.dumps(value, separators=(",", ":")).encode())
        path.write_bytes(blob)
        monkeypatch.setattr(cache, "CACHE_SHA256", hashlib.sha256(blob).hexdigest())

    install(document)
    return catalog, path, document, install, expected


def test_complete_order_and_ambiguity_preserved(small_cache):
    catalog, _, _, _, expected = small_cache
    actual = cache.load_index(ENTRIES, catalog.parent / "." / catalog.name)
    assert tuple(actual.items()) == tuple(expected.items())
    assert actual[diagnosis_terms.meaning_key("Pain right hand")] == ("A001", "A003")


def test_warm_reuse_still_validates_current_inputs(small_cache, monkeypatch):
    catalog, path, _, _, expected = small_cache
    cache._cached_decode.cache_clear()
    decode = cache.decode_index
    calls = []
    monkeypatch.setattr(cache, "decode_index", lambda *args: (calls.append(True), decode(*args))[1])
    first = cache.load_index(ENTRIES, catalog)
    assert first == expected
    assert cache.load_index(tuple(list(ENTRIES)), catalog) is first
    assert len(calls) == 1
    original_catalog, original_cache = catalog.read_bytes(), path.read_bytes()
    catalog.write_bytes(b"changed source after warm load")
    assert cache.load_index(ENTRIES, catalog) is None
    catalog.write_bytes(original_catalog)
    path.write_bytes(b"changed payload after warm load")
    assert cache.load_index(ENTRIES, catalog) is None
    path.write_bytes(original_cache)
    with monkeypatch.context() as patch:
        patch.setattr(diagnosis_terms, "VERSION", "changed after warm load")
        assert cache.load_index(ENTRIES, catalog) is None
    with monkeypatch.context() as patch:
        patch.setattr(diagnosis_terms, "__file__", str(catalog))
        assert cache.load_index(ENTRIES, catalog) is None
    assert len(calls) == 1  # Changed source/payload is rejected before memo access.
    with monkeypatch.context() as patch:
        patch.setattr(cache, "FORMAT", "changed decoder contract")
        assert cache.load_index(ENTRIES, catalog) is None
    assert len(calls) == 2  # Metadata changes cannot reuse prior decoder acceptance.
    assert cache.load_index(ENTRIES, catalog) is first
    assert cache._cached_decode.cache_info().maxsize == 4


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(extra=True),
    lambda d: d.pop("format"),
    lambda d: d.update(format="other"),
    lambda d: d.update(generatorVersion="other"),
    lambda d: d.update(generatorSha256="0" * 64),
    lambda d: d.update(catalogSha256="0" * 64),
    lambda d: d.update(termsSourceSha256="0" * 64),
    lambda d: d.update(termsVersion="other"),
    lambda d: d.update(entriesSha256="0" * 64),
    lambda d: d.update(entryCount=True),
    lambda d: d["rows"].reverse(),
    lambda d: d["rows"][0][2].reverse(),
    lambda d: d["rows"][0][2].append("A001"),
    lambda d: d["rows"][0][2].append("Z999"),
    lambda d: d["rows"][0][2].pop(),
    lambda d: d["rows"][0][0].append("absent:wrong"),
    lambda d: d["rows"][0][0].append("x" * 129),
    lambda d: d["rows"][1][1].append(d["rows"][1][1][0]),
    lambda d: d["rows"].append(d["rows"][0]),
    lambda d: d["rows"].__setitem__(0, {}),
])
def test_malformed_trusted_payload_falls_back(small_cache, mutation):
    catalog, _, document, install, _ = small_cache
    malformed = copy.deepcopy(document)
    mutation(malformed)
    install(malformed)
    assert cache.load_index(ENTRIES, catalog) is None


def test_valid_but_changed_meanings_fail_payload_pin(small_cache):
    catalog, path, document, _, _ = small_cache
    document["rows"][0][0] = ["different", "meaning"]
    path.write_bytes(_gzip(json.dumps(document).encode()))
    assert cache.load_index(ENTRIES, catalog) is None


@pytest.mark.parametrize("body", [b"not gzip", _gzip(b"["), _gzip(b'{"format":1,"format":2}')])
def test_invalid_compression_or_json_falls_back(small_cache, monkeypatch, body):
    catalog, path, _, _, _ = small_cache
    path.write_bytes(body)
    monkeypatch.setattr(cache, "CACHE_SHA256", hashlib.sha256(body).hexdigest())
    assert cache.load_index(ENTRIES, catalog) is None


@pytest.mark.parametrize("bound", ["MAX_COMPRESSED", "MAX_DECOMPRESSED", "MAX_TERMS", "MAX_ENTRIES"])
def test_input_bounds(small_cache, monkeypatch, bound):
    catalog, _, _, _, _ = small_cache
    monkeypatch.setattr(cache, bound, 1)
    assert cache.load_index(ENTRIES, catalog) is None


def test_missing_custom_stale_sources_fall_back(small_cache, tmp_path, monkeypatch):
    catalog, path, _, _, _ = small_cache
    other = tmp_path / "custom.csv.gz"
    other.write_bytes(catalog.read_bytes())
    assert cache.load_index(ENTRIES, other) is None
    assert cache.load_index(tuple(reversed(ENTRIES)), catalog) is None
    assert cache.load_index((*ENTRIES[:-1], ("A004", "Changed description")), catalog) is None
    with monkeypatch.context() as patch:
        patch.setattr(diagnosis_terms, "VERSION", "other")
        assert cache.load_index(ENTRIES, catalog) is None
    with monkeypatch.context() as patch:
        patch.setattr(diagnosis_terms, "__file__", str(other))
        assert cache.load_index(ENTRIES, catalog) is None
    original = catalog.read_bytes()
    catalog.write_bytes(b"changed")
    assert cache.load_index(ENTRIES, catalog) is None
    catalog.write_bytes(original)
    path.unlink()
    assert cache.load_index(ENTRIES, catalog) is None


def test_custom_catalog_uses_original_builder(tmp_path, monkeypatch):
    ccsr = tmp_path / "ccsr.csv"
    ccsr.write_text('code,description,ccsr_category,ccsr_description\nA001,Old label,X,X\n')
    current = tmp_path / "current.csv"
    current.write_text('A001,1,short,Pain right hand\nA003,1,short,Pain right hand\n')
    kb = icd10_ccsr.ICD10CCSR(csv_path=ccsr, current_csv=current, settings=Settings())
    builder = icd10_ccsr._description_index
    calls = []
    monkeypatch.setattr(icd10_ccsr, "_description_index", lambda entries: (calls.append(entries), builder(entries))[1])
    kb._ensure_index()
    assert calls == [(('A001', 'Pain right hand'), ('A003', 'Pain right hand'))]
    assert kb.resolve("Pain right hand")[0] is None  # Full ambiguity remains held.


def test_full_generated_bundle_exactly_matches_original_index(monkeypatch):
    assert cache.CACHE_SHA256 and cache.CACHE.is_file(), "Generate, verify and pin the artifact first"
    kb = icd10_ccsr.ICD10CCSR(settings=Settings())
    entries = tuple((raw, desc) for raw, (billable, desc) in kb._current.items() if billable)
    expected = icd10_ccsr._description_index(entries)
    monkeypatch.setattr(icd10_ccsr, "_description_index", lambda _: pytest.fail("bundled fast path fell back"))
    kb._ensure_index()
    assert tuple(kb._terminology_index.items()) == tuple(expected.items())
    assert sum(len(raws) for raws in expected.values()) == len(entries)
    assert cache.CACHE.read_bytes()[4:8] == b"\0\0\0\0"  # Reproducible gzip timestamp.
