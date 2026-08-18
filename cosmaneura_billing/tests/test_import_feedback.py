"""Legacy feedback import is explicit, deterministic, and prevalidated."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from cosmaneura_billing import import_feedback as importer
from cosmaneura_billing.settings import Settings


TENANT_ID = "11111111-1111-4111-8111-111111111111"
CODER_ID = "22222222-2222-4222-8222-222222222222"


def _row(*, coder: str = "legacy-coder", action: str = "reject") -> dict:
    return {
        "encounter_id": "SYNTHETIC-ENCOUNTER-1",
        "coder_id": coder,
        "engine_version": "legacy-test",
        "submitted_em": "99213",
        "engine_em": "99213",
        "final_em": "99213",
        "decisions": [{
            "kind": "ICD10",
            "engine_code": "K59.00",
            "action": action,
            "final_code": None,
            "reason_code": "dx_not_supported_by_doc",
            "note": "synthetic",
        }],
        "time_spent_seconds": 12,
        "overall_note": "synthetic",
        "ts": 1_700_000_000,
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_prepare_import_maps_identity_and_derives_stable_retry_ids(tmp_path):
    source = tmp_path / "feedback.jsonl"
    _write_jsonl(source, [_row(), _row()])

    digest, first = importer.prepare_import(
        source,
        tenant_id=TENANT_ID,
        coder_mapping={"legacy-coder": CODER_ID},
    )
    repeated_digest, repeated = importer.prepare_import(
        source,
        tenant_id=TENANT_ID,
        coder_mapping={"legacy-coder": CODER_ID},
    )

    assert digest == repeated_digest
    assert [feedback.review_id for feedback, _, _ in first] == [
        feedback.review_id for feedback, _, _ in repeated
    ]
    assert first[0][0].review_id != first[1][0].review_id
    assert all(UUID(feedback.review_id) for feedback, _, _ in first)
    assert all(feedback.coder_id == CODER_ID for feedback, _, _ in first)
    assert all(feedback.coder_tenant_id == TENANT_ID for feedback, _, _ in first)
    assert [line_number for _, _, line_number in first] == [1, 2]
    assert all(source_digest.hex() == digest for _, source_digest, _ in first)


@pytest.mark.parametrize(
    ("rows", "mapping", "message"),
    [
        ([_row(), _row(coder="unknown")], {"legacy-coder": CODER_ID}, "line 2"),
        ([_row(action="upsert")], {"legacy-coder": CODER_ID}, "line 1"),
    ],
)
def test_prepare_import_rejects_entire_file_when_any_row_is_untrusted(
    tmp_path, rows, mapping, message,
):
    source = tmp_path / "feedback.jsonl"
    _write_jsonl(source, rows)

    with pytest.raises(ValueError, match=message):
        importer.prepare_import(
            source,
            tenant_id=TENANT_ID,
            coder_mapping=mapping,
        )


@pytest.mark.parametrize(
    "invalid_case",
    [
        "action_shape",
        "reason_vocabulary",
        "decision_note_size",
        "header_bounds",
    ],
)
def test_prepare_import_enforces_the_live_feedback_contract(
    tmp_path, invalid_case,
):
    source = tmp_path / "feedback.jsonl"
    row = _row()
    if invalid_case == "action_shape":
        row["decisions"][0]["engine_code"] = None
    elif invalid_case == "reason_vocabulary":
        row["decisions"][0]["reason_code"] = "operator_free_text"
    elif invalid_case == "decision_note_size":
        row["decisions"][0]["note"] = "x" * 4_001
    else:
        row["time_spent_seconds"] = 86_401
    _write_jsonl(source, [row])

    with pytest.raises(ValueError, match="line 1"):
        importer.prepare_import(
            source,
            tenant_id=TENANT_ID,
            coder_mapping={"legacy-coder": CODER_ID},
        )


def test_invalid_late_row_is_rejected_before_any_database_dependency(
    tmp_path, monkeypatch,
):
    source = tmp_path / "feedback.jsonl"
    mapping = tmp_path / "coders.json"
    _write_jsonl(source, [_row(), _row(coder="unknown")])
    mapping.write_text(json.dumps({"legacy-coder": CODER_ID}), encoding="utf-8")

    def unexpected_database_access(*_args, **_kwargs):
        raise AssertionError("prevalidation must finish before database access")

    monkeypatch.setattr(importer, "PostgresContext", unexpected_database_access)
    monkeypatch.setattr(importer, "PostgresFeedbackStore", unexpected_database_access)

    with pytest.raises(ValueError, match="line 2"):
        importer.import_feedback(
            source,
            mapping,
            settings=Settings(
                database_url="postgresql://unused.invalid/db",
                entra_tenant_id=TENANT_ID,
                practice_id="practice-a",
            ),
        )


def test_dry_run_validates_without_constructing_a_database_store(
    tmp_path, monkeypatch,
):
    source = tmp_path / "feedback.jsonl"
    mapping = tmp_path / "coders.json"
    _write_jsonl(source, [_row()])
    mapping.write_text(json.dumps({"legacy-coder": CODER_ID}), encoding="utf-8")

    def unexpected_database_access(*_args, **_kwargs):
        raise AssertionError("dry-run must not construct a database dependency")

    monkeypatch.setattr(importer, "PostgresContext", unexpected_database_access)
    monkeypatch.setattr(importer, "PostgresFeedbackStore", unexpected_database_access)

    result = importer.import_feedback(
        source,
        mapping,
        settings=Settings(
            database_url="postgresql://unused.invalid/db",
            entra_tenant_id=TENANT_ID,
            practice_id="practice-a",
        ),
        dry_run=True,
    )

    assert result == {
        "digest": result["digest"],
        "validated": 1,
        "imported": 0,
        "replayed": 0,
    }
    assert len(result["digest"]) == 64


@pytest.mark.parametrize(
    "mapping_payload",
    [[], {"legacy-coder": 123}],
)
def test_mapping_file_must_be_a_string_to_string_object(
    tmp_path, mapping_payload,
):
    source = tmp_path / "feedback.jsonl"
    mapping = tmp_path / "coders.json"
    _write_jsonl(source, [_row()])
    mapping.write_text(json.dumps(mapping_payload), encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object of string IDs"):
        importer.import_feedback(
            source,
            mapping,
            settings=Settings(entra_tenant_id=TENANT_ID),
            dry_run=True,
        )
