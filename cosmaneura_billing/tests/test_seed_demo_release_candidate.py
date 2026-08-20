"""Fixed-purpose, fail-closed synthetic release-candidate pack seeding."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from cosmaneura_billing import seed_demo_release_candidate as seeder
from cosmaneura_billing.settings import Settings


TENANT_ID = "11111111-1111-4111-8111-111111111111"
PRACTICE_ID = "practice-a"


def _settings(**changes) -> Settings:
    values = {
        "auth_mode": "container_apps",
        "database_url": "postgresql://cosmaneura_app:secret@db.invalid/app",
        "entra_tenant_id": TENANT_ID,
        "practice_id": PRACTICE_ID,
        "state_store": "postgres",
    }
    values.update(changes)
    return Settings(**values)


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return deepcopy(self._row)


class _Connection:
    def __init__(self, database):
        self.database = database
        self.insert_parameters = []

    def execute(self, sql, parameters):
        compact = " ".join(sql.split())
        if "FROM practices AS p" in compact:
            practice_id, tenant_id = parameters
            binding = self.database["binding"]
            if (
                practice_id != binding["practice_id"]
                or tenant_id != binding["tenant_id"]
                or not binding["active"]
            ):
                return _Result(None)
            return _Result({
                **binding,
                "selected_practice_id": practice_id,
                "practice_allowed": self.database["practice_allowed"],
            })
        if compact.startswith("INSERT INTO encounters"):
            assert "ON CONFLICT (practice_id, external_id) DO NOTHING" in compact
            assert "DO UPDATE" not in compact
            self.insert_parameters.append(parameters)
            external_id = parameters[1]
            if external_id in self.database["encounters"]:
                return _Result(None)
            keys = (
                "practice_id",
                "external_id",
                "patient_age",
                "patient_sex",
                "encounter_type",
                "place_of_service",
                "intake_text",
                "transcript",
                "signed_note",
                "provider_specialty",
                "submitted_em_code",
                "submitted_icd10",
                "source",
            )
            created = dict(zip(keys, parameters))
            created.update({
                "ingested_by_tenant_id": None,
                "ingested_by_user_id": None,
                "version": 1,
            })
            self.database["encounters"][external_id] = created
            return _Result({"external_id": external_id})
        if "FROM encounters" in compact:
            practice_id, external_id = parameters
            row = self.database["encounters"].get(external_id)
            if row is None or row["practice_id"] != practice_id:
                return _Result(None)
            persisted = dict(row)
            persisted.pop("practice_id", None)
            return _Result(persisted)
        raise AssertionError(f"unexpected SQL: {compact}")


class _ConnectionScope:
    def __init__(self, database, connection):
        self.database = database
        self.connection = connection
        self.before = None

    def __enter__(self):
        self.before = deepcopy(self.database["encounters"])
        return self.connection

    def __exit__(self, exception_type, *_args):
        if exception_type is not None:
            # Model psycopg's transaction context: an exception rolls back all
            # prior statements issued by this invocation.
            self.database["encounters"] = self.before
        return False


class _Context:
    def __init__(self, database, dsn, *, practice_id):
        assert dsn == "postgresql://cosmaneura_app:secret@db.invalid/app"
        assert practice_id == PRACTICE_ID
        self.database = database
        self.connection_object = _Connection(database)

    def connection(self):
        return _ConnectionScope(self.database, self.connection_object)


@pytest.fixture
def fake_postgres(monkeypatch):
    database = {
        "binding": {
            "practice_id": PRACTICE_ID,
            "tenant_id": TENANT_ID,
            "active": True,
        },
        "practice_allowed": True,
        "encounters": {},
    }
    contexts = []

    def context_factory(dsn, *, practice_id):
        context = _Context(database, dsn, practice_id=practice_id)
        contexts.append(context)
        return context

    monkeypatch.setattr(seeder, "PostgresContext", context_factory)
    return database, contexts


def test_prepared_input_is_the_complete_digest_pinned_pack():
    first = seeder.prepare_synthetic_demo_seed()
    second = seeder.prepare_synthetic_demo_seed()

    assert first == second
    assert tuple(case.case_id for case in first.cases) == seeder.CASE_IDS
    assert tuple(case.visit["encounter_id"] for case in first.cases) == (
        "SYN-RC-0001",
        "SYN-RC-0002",
        "SYN-RC-0003",
        "SYN-RC-0004",
    )
    assert seeder.PACK_ID == "cosmaneura-demo-rc-v1"
    assert seeder.SOURCE == "synthetic_demo_release_candidate"
    assert first.digest == seeder.EXPECTED_PACK_DIGEST
    assert {case.case_id: case.digest for case in first.cases} == (
        seeder.EXPECTED_CASE_DIGESTS
    )
    for digest in (first.digest, *seeder.EXPECTED_CASE_DIGESTS.values()):
        assert len(digest) == 64
        assert set(digest) <= set("0123456789abcdef")


def test_changed_pack_metadata_fails_before_database_context(monkeypatch):
    changed = deepcopy(seeder.load_demo_release_candidate())
    changed["synthetic_declaration"] += " Changed metadata."
    monkeypatch.setattr(seeder, "load_demo_release_candidate", lambda: changed)

    def unexpected_context(*_args, **_kwargs):
        raise AssertionError("database access must remain closed")

    monkeypatch.setattr(seeder, "PostgresContext", unexpected_context)
    with pytest.raises(seeder.SyntheticDemoSeedError, match="pack digest changed"):
        seeder.seed_synthetic_demo_release_candidate(_settings())


def test_changed_case_content_fails_its_fixed_digest(monkeypatch):
    changed = deepcopy(seeder.load_demo_release_candidate())
    changed["cases"][2]["visit"]["intake_text"] += " Synthetic change."
    # Prove the independent per-case pin after allowing this intentionally
    # modified whole-pack digest through the first guard in this unit test.
    monkeypatch.setattr(seeder, "EXPECTED_PACK_DIGEST", seeder._canonical_digest(changed))
    monkeypatch.setattr(seeder, "load_demo_release_candidate", lambda: changed)

    with pytest.raises(seeder.SyntheticDemoSeedError, match="case digest changed"):
        seeder.prepare_synthetic_demo_seed()


def test_first_seed_inserts_all_cases_and_exact_replay_is_a_noop(fake_postgres):
    database, contexts = fake_postgres

    inserted = seeder.seed_synthetic_demo_release_candidate(_settings())
    snapshot = deepcopy(database["encounters"])
    replayed = seeder.seed_synthetic_demo_release_candidate(_settings())

    assert inserted == {
        "pack_id": seeder.PACK_ID,
        "case_count": 4,
        "inserted_count": 4,
        "replayed_count": 0,
        "status": "inserted",
        "digest": seeder.EXPECTED_PACK_DIGEST,
    }
    assert replayed == {
        **inserted,
        "inserted_count": 0,
        "replayed_count": 4,
        "status": "replayed",
    }
    assert database["encounters"] == snapshot
    assert set(database["encounters"]) == set(seeder.CASE_IDS)
    assert all(
        row["source"] == seeder.SOURCE
        for row in database["encounters"].values()
    )
    assert [params[1] for params in contexts[0].connection_object.insert_parameters] == (
        list(seeder.CASE_IDS)
    )


def test_mixed_exact_replay_and_new_cases_reports_closed_counts(fake_postgres):
    database, _ = fake_postgres
    seeder.seed_synthetic_demo_release_candidate(_settings())
    del database["encounters"][seeder.CASE_IDS[1]]
    del database["encounters"][seeder.CASE_IDS[3]]

    result = seeder.seed_synthetic_demo_release_candidate(_settings())

    assert result["status"] == "inserted"
    assert result["case_count"] == 4
    assert result["inserted_count"] == 2
    assert result["replayed_count"] == 2
    assert set(database["encounters"]) == set(seeder.CASE_IDS)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("signed_note", "different synthetic content"),
        ("source", "synthetic"),
        ("version", 2),
    ],
)
def test_conflict_rolls_back_prior_insert_without_overwrite(
    fake_postgres, field, replacement,
):
    database, contexts = fake_postgres
    seeder.seed_synthetic_demo_release_candidate(_settings())
    # Force case 2 to be inserted before case 3 reveals its conflict. Case 4
    # is also absent, making any accidental partial commit easy to observe.
    del database["encounters"][seeder.CASE_IDS[1]]
    database["encounters"][seeder.CASE_IDS[2]][field] = replacement
    del database["encounters"][seeder.CASE_IDS[3]]
    snapshot = deepcopy(database["encounters"])

    with pytest.raises(seeder.SyntheticDemoConflict):
        seeder.seed_synthetic_demo_release_candidate(_settings())

    assert database["encounters"] == snapshot
    failed_connection = contexts[-1].connection_object
    assert seeder.CASE_IDS[1] in [
        parameters[1] for parameters in failed_connection.insert_parameters
    ]


@pytest.mark.parametrize("binding_failure", ["missing", "inactive", "denied"])
def test_unproven_practice_binding_fails_before_insert(
    fake_postgres, binding_failure,
):
    database, contexts = fake_postgres
    if binding_failure == "missing":
        database["binding"]["tenant_id"] = "22222222-2222-4222-8222-222222222222"
    elif binding_failure == "inactive":
        database["binding"]["active"] = False
    else:
        database["practice_allowed"] = False

    with pytest.raises(seeder.SyntheticDemoPracticeBindingError):
        seeder.seed_synthetic_demo_release_candidate(_settings())

    assert database["encounters"] == {}
    assert contexts[0].connection_object.insert_parameters == []


@pytest.mark.parametrize("database_url", ["", "   "])
def test_missing_postgres_dsn_never_constructs_a_database_context(
    monkeypatch, database_url,
):
    def unexpected_context(*_args, **_kwargs):
        raise AssertionError("database access must remain closed")

    monkeypatch.setattr(seeder, "PostgresContext", unexpected_context)
    with pytest.raises(seeder.SyntheticDemoPracticeBindingError):
        seeder.seed_synthetic_demo_release_candidate(Settings(
            database_url=database_url,
            entra_tenant_id=TENANT_ID,
            practice_id=PRACTICE_ID,
        ))


def test_cli_rejects_all_arguments_without_reflecting_them(capsys, monkeypatch):
    def unexpected_seed(*_args, **_kwargs):
        raise AssertionError("arguments must be rejected before database access")

    monkeypatch.setattr(seeder, "seed_synthetic_demo_release_candidate", unexpected_seed)
    secret_argument = "patient-name-that-must-not-appear"

    assert seeder.main([secret_argument]) == 2

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert output.err == ""
    assert secret_argument not in output.out
    assert set(payload) == {
        "pack_id",
        "case_count",
        "inserted_count",
        "replayed_count",
        "status",
        "digest",
    }
    assert payload == {
        "pack_id": seeder.PACK_ID,
        "case_count": 4,
        "inserted_count": 0,
        "replayed_count": 0,
        "status": "arguments_rejected",
        "digest": seeder.EXPECTED_PACK_DIGEST,
    }


def test_cli_failure_output_is_closed_and_phi_free(capsys, monkeypatch):
    sensitive = "postgresql://user:password@db.example/real Patient Example"

    def fail_closed(*_args, **_kwargs):
        raise RuntimeError(sensitive)

    monkeypatch.setattr(seeder, "seed_synthetic_demo_release_candidate", fail_closed)
    monkeypatch.setattr(seeder.Settings, "from_env", classmethod(lambda cls: _settings()))

    assert seeder.main([]) == 1

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert output.err == ""
    assert sensitive not in output.out
    assert set(payload) == {
        "pack_id",
        "case_count",
        "inserted_count",
        "replayed_count",
        "status",
        "digest",
    }
    assert payload["status"] == "failed"
    assert payload["case_count"] == 4
    assert payload["inserted_count"] == payload["replayed_count"] == 0
