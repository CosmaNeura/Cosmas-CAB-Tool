"""CPT descriptor loader — runs only on the practice's AMA-licensed files.
Tests use synthetic placeholder data, NOT real CPT descriptors."""
from cosmaneura_billing.knowledge.cpt import CPTDescriptors
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion

FAKE_SDF = """\
99213 FAKE-DESCRIPTOR-A est patient visit
99214 FAKE-DESCRIPTOR-B est patient visit
0001U FAKE-DESCRIPTOR-C lab test
"""


def _clean_env(monkeypatch):
    for v in ("CPT_DATA_DIR", "AMA_TOKEN_URL", "AMA_BASE_URL",
              "AMA_CONSUMER_KEY", "AMA_CONSUMER_SECRET"):
        monkeypatch.delenv(v, raising=False)


def test_not_loaded_without_data(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    cpt = CPTDescriptors(cache_dir=tmp_path / "cache")
    assert not cpt.loaded
    assert cpt.get("99213") is None
    assert cpt.relabel([CodeSuggestion(kind=CodeKind.EM, code="99213", label="x")]) == 0


def test_loads_standard_data_file_layout(tmp_path):
    f = tmp_path / "SHORTU.txt"
    f.write_text(FAKE_SDF)
    cpt = CPTDescriptors(f)
    assert cpt.loaded and cpt.size == 3
    assert cpt.get("99214") == "FAKE-DESCRIPTOR-B est patient visit"
    assert cpt.get("0001U") == "FAKE-DESCRIPTOR-C lab test"


def test_resolves_from_env_dir(tmp_path, monkeypatch):
    (tmp_path / "SHORTU.txt").write_text(FAKE_SDF)
    monkeypatch.setenv("CPT_DATA_DIR", str(tmp_path))
    cpt = CPTDescriptors()
    assert cpt.loaded and cpt.source.endswith("SHORTU.txt")


def test_tab_delimited_export(tmp_path):
    f = tmp_path / "cpt.txt"
    f.write_text("99213\tFAKE-TAB-DESC\n99215\tFAKE-TAB-DESC-2\n")
    cpt = CPTDescriptors(f)
    assert cpt.get("99215") == "FAKE-TAB-DESC-2"


def test_relabel_upgrades_generic_labels(tmp_path):
    f = tmp_path / "SHORTU.txt"
    f.write_text(FAKE_SDF)
    cpt = CPTDescriptors(f)
    em = CodeSuggestion(kind=CodeKind.EM, code="99214", label="Office visit, generic label")
    unknown = CodeSuggestion(kind=CodeKind.PROCEDURE, code="99999", label="keep me")
    assert cpt.relabel([em, unknown]) == 1
    assert em.label == "FAKE-DESCRIPTOR-B est patient visit"
    assert unknown.label == "keep me"


# ---- auto-fetch from the AMA CPT Zip API (all HTTP faked) ------------------- #
def _fake_ama(monkeypatch, tmp_path, token_status=200, redirect=False):
    """Fake the token, releases, and file endpoints behind the module seam."""
    import io as _io
    import json as _json
    import urllib.error
    import zipfile as _zip

    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr("2026 Update/Legacy Format/SHORTU.txt", FAKE_SDF)
        zf.writestr("2026 Update/Appendix.pdf", b"%PDF-fake")
    zip_bytes = buf.getvalue()

    class R:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = []

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if "oauth2/token" in url:
            if token_status != 200:
                raise urllib.error.HTTPError(url, token_status, "denied", {}, _io.BytesIO(b""))
            assert req.headers.get("Authorization", "").startswith("Basic ")
            return R(_json.dumps({"access_token": "tok-1", "expires_in": 3600}).encode())
        if url.endswith("/releases"):
            return R(_json.dumps({"releases": [{"id": "cpt-2026"}]}).encode())
        if "/files" in url and "s3.example" not in url:
            if redirect:
                raise urllib.error.HTTPError(
                    url, 303, "See Other", {"Location": "https://s3.example/presigned"},
                    _io.BytesIO(b""))
            return R(zip_bytes)
        if "s3.example" in url:
            assert "Authorization" not in req.headers, "token must not go to storage"
            return R(zip_bytes)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("cosmaneura_billing.knowledge.cpt._urlopen", fake_urlopen)
    monkeypatch.setenv("AMA_TOKEN_URL", "https://gw.example/oauth2/token")
    monkeypatch.setenv("AMA_BASE_URL", "https://gw.example")
    monkeypatch.setenv("AMA_CONSUMER_KEY", "k")
    monkeypatch.setenv("AMA_CONSUMER_SECRET", "s")
    monkeypatch.delenv("CPT_DATA_DIR", raising=False)
    return calls


def test_auto_fetch_with_credentials(monkeypatch, tmp_path):
    calls = _fake_ama(monkeypatch, tmp_path)
    cpt = CPTDescriptors(cache_dir=tmp_path / "cache")
    assert cpt.loaded and cpt.size == 3
    assert cpt.get("99214") == "FAKE-DESCRIPTOR-B est patient visit"
    assert any("files?release=cpt-2026" in c for c in calls)
    # only text data extracted, never the PDF bulk
    assert not list((tmp_path / "cache").rglob("*.pdf"))


def test_auto_fetch_follows_presigned_redirect_without_token(monkeypatch, tmp_path):
    calls = _fake_ama(monkeypatch, tmp_path, redirect=True)
    cpt = CPTDescriptors(cache_dir=tmp_path / "cache")
    assert cpt.loaded and cpt.size == 3
    assert any("s3.example" in c for c in calls)


def test_auto_fetch_uses_cache_on_second_load(monkeypatch, tmp_path):
    _fake_ama(monkeypatch, tmp_path)
    CPTDescriptors(cache_dir=tmp_path / "cache")
    # second instance must load from cache without any HTTP
    monkeypatch.setattr("cosmaneura_billing.knowledge.cpt._urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network hit")))
    cpt2 = CPTDescriptors(cache_dir=tmp_path / "cache")
    assert cpt2.loaded and cpt2.size == 3


def test_auto_fetch_failure_is_non_fatal(monkeypatch, tmp_path):
    _fake_ama(monkeypatch, tmp_path, token_status=401)
    cpt = CPTDescriptors(cache_dir=tmp_path / "cache")
    assert not cpt.loaded
    assert cpt.fetch_error and "401" in cpt.fetch_error


def test_auto_fetch_disabled_by_env(monkeypatch, tmp_path):
    calls = _fake_ama(monkeypatch, tmp_path)
    monkeypatch.setenv("CPT_AUTO_FETCH", "0")
    cpt = CPTDescriptors(cache_dir=tmp_path / "cache")
    assert not cpt.loaded and calls == []
