"""CPT descriptor loader — runs only on the practice's AMA-licensed files.
Tests use synthetic placeholder data, NOT real CPT descriptors."""
from cosmaneura_billing.knowledge.cpt import CPTDescriptors
from cosmaneura_billing.schemas import CodeKind, CodeSuggestion

FAKE_SDF = """\
99213 FAKE-DESCRIPTOR-A est patient visit
99214 FAKE-DESCRIPTOR-B est patient visit
0001U FAKE-DESCRIPTOR-C lab test
"""


def test_not_loaded_without_data(monkeypatch):
    monkeypatch.delenv("CPT_DATA_DIR", raising=False)
    cpt = CPTDescriptors()
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
