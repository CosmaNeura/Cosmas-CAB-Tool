"""The fixed MCD command admits only its pinned source and closed output."""
from copy import deepcopy
import json

import pytest

from cosmaneura_billing import seed_mcd_training as seeder
from cosmaneura_billing import seed_demo_release_candidate as existing
from test_seed_demo_release_candidate import _settings, fake_postgres


def test_mcd_and_existing_seed_coexist_and_retry_without_changes(fake_postgres):
    database, _ = fake_postgres
    existing.seed_synthetic_demo_release_candidate(_settings())
    old_rows = deepcopy(database["encounters"])
    first = seeder.seed_mcd_training(_settings())
    all_rows = deepcopy(database["encounters"])
    replay = seeder.seed_mcd_training(_settings())
    assert first == {
        "pack_id": seeder.PACK_ID, "case_count": 12, "inserted_count": 12,
        "replayed_count": 0, "status": "inserted", "digest": seeder.EXPECTED_SEED_DIGEST,
    }
    assert replay == {**first, "inserted_count": 0, "replayed_count": 12, "status": "replayed"}
    assert database["encounters"] == all_rows
    assert {key: all_rows[key] for key in old_rows} == old_rows
    assert len(all_rows) == 24


@pytest.mark.parametrize("change", ["text", "identity", "order", "extra_field"])
def test_changed_loader_result_never_opens_database(monkeypatch, change):
    pack = seeder.load_mcd_training_pack()
    if change == "text":
        pack["cases"][-1]["visit"]["signed_note"] += " Synthetic alteration."
    elif change == "identity":
        pack["cases"][-1]["visit"]["encounter_id"] = "SYN-CAB-DIFFERENT"
    elif change == "order":
        pack["cases"].reverse()
    else:
        pack["cases"][-1]["unexpected"] = "ignored metadata is forbidden"
    monkeypatch.setattr(seeder, "load_mcd_training_pack", lambda: pack)
    opened = []
    monkeypatch.setattr(existing, "PostgresContext", lambda *a, **k: opened.append(True))
    with pytest.raises(existing.SyntheticDemoSeedError):
        seeder.seed_mcd_training(_settings())
    assert opened == []


@pytest.mark.parametrize("arguments", [["--file", "private-input.json"], ["--help"], ["sensitive-value"]])
def test_cli_accepts_no_arguments_or_payload_and_does_not_reflect_them(monkeypatch, capsys, arguments):
    def unexpected_settings():
        raise AssertionError("must reject before reading settings")
    monkeypatch.setattr(seeder.Settings, "from_env", unexpected_settings)
    assert seeder.main(arguments) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert all(argument not in output.out for argument in arguments)
    assert json.loads(output.out) == seeder._closed_result("arguments_rejected")


@pytest.mark.parametrize(("error", "status"), [
    (existing.SyntheticDemoConflict("private-detail"), "conflict"),
    (existing.SyntheticDemoDatabaseAuthorityError("private-detail"), "binding_rejected"),
    (existing.SyntheticDemoPracticeBindingError("private-detail"), "binding_rejected"),
    (RuntimeError("private-detail"), "failed"),
])
def test_cli_failure_has_only_closed_metadata(monkeypatch, capsys, error, status):
    monkeypatch.setattr(seeder.Settings, "from_env", _settings)
    def fail(_settings):
        raise error
    monkeypatch.setattr(seeder, "seed_mcd_training", fail)
    assert seeder.main([]) == 1
    output = capsys.readouterr()
    assert output.err == ""
    assert "private-detail" not in output.out
    assert json.loads(output.out) == seeder._closed_result(status)
