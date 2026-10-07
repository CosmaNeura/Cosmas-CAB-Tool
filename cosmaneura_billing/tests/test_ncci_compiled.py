"""Reviewed bundled derivative and the unchanged custom-file fallback boundary."""
import copy
import gzip
import hashlib
import json

import pytest

from cosmaneura_billing.knowledge import ncci as module


@pytest.fixture()
def compiled(tmp_path, monkeypatch):
    ptp = tmp_path / "ptp_practitioner_2026q3_v322.tsv.gz"
    mue = tmp_path / "mue_practitioner_2026q3.tsv.gz"
    ptp.write_bytes(gzip.compress(
        b"Column 1\tColumn 2\tModifier\n99.213\t36415\t1\n99213\t36415\t0\n"
        b"36415\t99213\t1\nA1234\tB1234\t\nC1234\tD1234\tx-other\n", mtime=0))
    mue.write_bytes(gzip.compress(
        b"HCPCS/CPT Code\tPractitioner MUE Value\nA1234\t3.9\nA1234\t2\n\t4\n", mtime=0))
    sources = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (ptp, mue)}
    counts = {"ptpLoadedRows": 5, "ptpPairs": 4, "mueLoadedRows": 3, "mueCodes": 2}
    codes = ["", "36415", "99213", "A1234", "B1234", "C1234", "D1234"]
    document = {"schemaVersion": module._COMPILED_SCHEMA,
        "normalizationVersion": module._NORMALIZATION_VERSION,
        "build": dict(module._COMPILED_BUILD), "sources": sources,
        "counts": {**counts, "codeStrings": len(codes)}, "codes": codes,
        "ptp": {"99213": {"36415": "0"}, "36415": {"99213": "1"},
            "A1234": {"B1234": "9"}, "C1234": {"D1234": "x"}},
        "mue": {"A1234": 2, "": 4}}
    cache = tmp_path / module._COMPILED_NAME

    def write(value):
        cache.write_bytes(gzip.compress(json.dumps(value, sort_keys=True,
            separators=(",", ":")).encode(), mtime=0))
        monkeypatch.setattr(module, "_COMPILED_SHA256", hashlib.sha256(cache.read_bytes()).hexdigest())

    monkeypatch.setattr(module, "_COMPILED_SOURCES", sources)
    monkeypatch.setattr(module, "_COMPILED_COUNTS", counts)
    monkeypatch.setattr(module.NCCIEdits, "_bundled_cache", {})
    write(document)
    return tmp_path, ptp, mue, cache, document, write


def test_compiled_fast_path_preserves_queries_and_later_custom_loads(compiled, monkeypatch):
    directory, ptp, mue, unused, document, write = compiled
    baseline = module.NCCIEdits.from_files(ptp, mue)
    monkeypatch.setattr(module.NCCIEdits, "from_files", classmethod(
        lambda *a, **k: pytest.fail("valid compiled cache must avoid the text reader")))
    result = module.NCCIEdits.from_bundled(directory)
    assert result.stats == baseline.stats == "4 PTP pairs, 2 MUE limits"
    assert result.loaded is baseline.loaded is True
    for pair in [("99.213", "36415"), ("36415", "99213"), ("B1234", "A1234"),
                 ("C1234", "D1234"), ("missing", "else")]:
        assert result.ptp_check(*pair) == baseline.ptp_check(*pair)
    for code in ("a.1234", "", "missing"):
        assert result.mue_limit(code) == baseline.mue_limit(code)
    result.ptp_check("99213", "36415").modifier_indicator = "9"
    assert result.ptp_check("99213", "36415").modifier_indicator == "0"
    custom = directory / "updates.csv"
    custom.write_text("Column 1,Column 2,Modifier\n99213,36415,1\nX1234,A1234,0\n")
    assert result.load_ptp(custom) == baseline.load_ptp(custom) == 2
    assert result.ptp_check("99213", "36415") == baseline.ptp_check("99213", "36415")
    assert result.ptp_check("A1234", "X1234") == baseline.ptp_check("A1234", "X1234")
    assert result.stats == baseline.stats == "5 PTP pairs, 2 MUE limits"


def test_valid_compiled_and_source_checks_run_once_per_process(compiled, monkeypatch):
    directory, ptp, mue, cache, unused, write = compiled
    original = module._sha_file
    reads = []
    monkeypatch.setattr(module, "_sha_file", lambda path: (reads.append(path), original(path))[1])
    first = module.NCCIEdits.from_bundled(directory)
    assert module.NCCIEdits.from_bundled(directory) is first
    assert reads == [cache, ptp, mue]


@pytest.mark.parametrize("kind", ["missing", "cache_hash", "source_hash", "custom_names"])
def test_cache_unavailable_or_different_sources_use_original_readers(compiled, monkeypatch, kind):
    directory, ptp, mue, cache, unused, write = compiled
    if kind == "missing":
        cache.unlink()
    elif kind == "cache_hash":
        monkeypatch.setattr(module, "_COMPILED_SHA256", "0" * 64)
    elif kind == "source_hash":
        ptp.write_bytes(gzip.compress(b"Column 1\tColumn 2\tModifier\n11111\t22222\t1\n", mtime=0))
    else:
        ptp.rename(directory / "ptp_custom.tsv.gz")
        mue.rename(directory / "mue_custom.tsv.gz")
        ptp, mue = directory / "ptp_custom.tsv.gz", directory / "mue_custom.tsv.gz"
    expected = module.NCCIEdits.from_files(ptp, mue)
    result = module.NCCIEdits.from_bundled(directory)
    assert result.stats == expected.stats
    assert result.ptp_check("99213", "36415") == expected.ptp_check("99213", "36415")
    assert result.ptp_check("11111", "22222") == expected.ptp_check("11111", "22222")
    assert result.mue_limit("") == expected.mue_limit("")


@pytest.mark.parametrize("kind", ["extra", "version", "build", "sources", "counts",
    "boolean_count", "pair_total", "inner_shape", "mue_shape", "code_duplicate", "code_type"])
def test_hash_bound_header_and_shape_validation_falls_back(compiled, monkeypatch, kind):
    directory, unused, unused2, cache, original, write = compiled
    document = copy.deepcopy(original)
    if kind == "extra":
        document["unexpected"] = True
    elif kind == "version":
        document["normalizationVersion"] = "different"
    elif kind == "build":
        document["build"]["generatorVersion"] = "different"
    elif kind == "sources":
        document["sources"] = {}
    elif kind == "counts":
        document["counts"]["ptpLoadedRows"] += 1
    elif kind == "boolean_count":
        document["counts"]["mueCodes"] = True
    elif kind == "pair_total":
        document["ptp"]["99213"]["new"] = "1"
    elif kind == "inner_shape":
        document["ptp"]["99213"] = []
    elif kind == "mue_shape":
        document["mue"] = []
    elif kind == "code_duplicate":
        document["codes"][0] = document["codes"][1]
    else:
        document["codes"][0] = 1
    # Rebind the authored test checksum to reach structural checks. Production
    # accepts only the checked-in full-catalog checksum, never arbitrary JSON.
    write(document)
    sentinel = object()
    monkeypatch.setattr(module.NCCIEdits, "from_files", classmethod(lambda *a, **k: sentinel))
    assert module.NCCIEdits.from_bundled(directory) is sentinel


@pytest.mark.parametrize("kind", ["gzip", "json", "compressed_bound", "json_bound"])
def test_corrupt_or_over_limit_cache_falls_back_without_partial_result(compiled, monkeypatch, kind):
    directory, unused, unused2, cache, document, write = compiled
    if kind == "gzip":
        cache.write_bytes(b"not gzip")
    elif kind == "json":
        cache.write_bytes(gzip.compress(b"{broken", mtime=0))
    elif kind == "compressed_bound":
        monkeypatch.setattr(module, "_COMPRESSED_LIMIT", 1)
    else:
        monkeypatch.setattr(module, "_JSON_LIMIT", 1)
    monkeypatch.setattr(module, "_COMPILED_SHA256", hashlib.sha256(cache.read_bytes()).hexdigest())
    sentinel = object()
    monkeypatch.setattr(module.NCCIEdits, "from_files", classmethod(lambda *a, **k: sentinel))
    assert module.NCCIEdits.from_bundled(directory) is sentinel
