"""Mock-first Athena boundary: parsing, pagination, and HTTP hardening."""
from __future__ import annotations

import json
import socket
import urllib.error
from copy import deepcopy

import pytest

from cosmaneura_billing.athena_client import (
    MAX_PAGE_RECORDS,
    AthenaAuthenticationError,
    AthenaHTTPTransport,
    AthenaPartialResponseError,
    AthenaRequest,
    AthenaRequestRejected,
    AthenaRetryExhausted,
    AthenaSandboxClient,
    AthenaSchemaError,
    AthenaTimeoutError,
    AthenaUnknownFieldError,
    MockAthenaClient,
    NORMALIZED_SCHEMA_VERSION,
    parse_normalized_page,
    read_all_pages,
)
from cosmaneura_billing.corpus.demo_release_candidate import (
    load_demo_release_candidate,
)


BASE_URL = "https://reviewed-contract.invalid/api"


def _valid_record() -> dict:
    case = load_demo_release_candidate()["cases"][0]
    return {
        "source_record_id": "athena-synthetic-record-1",
        "source_version": "version-1",
        "visit": deepcopy(case["visit"]),
    }


def _payload(
    records: list[dict] | None = None,
    *,
    next_cursor: str | None = None,
    checkpoint: str = "checkpoint-1",
) -> dict:
    return {
        "schema_version": NORMALIZED_SCHEMA_VERSION,
        "records": [_valid_record()] if records is None else records,
        "next_cursor": next_cursor,
        "checkpoint": checkpoint,
    }


class _Response:
    def __init__(self, payload, *, headers=None):
        self._raw = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )
        self.headers = headers or {}

    def read(self, limit):
        return self._raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_mock_reads_all_four_cases_in_two_deterministic_pages():
    first = read_all_pages(MockAthenaClient(server_practice_id="practice-a"))
    second = read_all_pages(MockAthenaClient(server_practice_id="practice-a"))

    assert len(first.records) == 4
    assert first.page_count == 2
    assert first.replay_count == 0
    assert first.checkpoint == "mock-checkpoint-v1-4"
    assert [record.idempotency_key for record in first.records] == [
        record.idempotency_key for record in second.records
    ]
    assert all(
        record.target_practice_id == "practice-a" for record in first.records
    )
    assert [record.visit.encounter_id for record in first.records] == [
        "SYN-RC-0001",
        "SYN-RC-0002",
        "SYN-RC-0003",
        "SYN-RC-0004",
    ]


def test_record_and_page_representations_redact_identifiers_and_cursor():
    page = MockAthenaClient(server_practice_id="secret-practice").read_page()
    record_repr = repr(page.records[0])
    page_repr = repr(page)
    result_repr = repr(
        read_all_pages(MockAthenaClient(server_practice_id="secret-practice"))
    )

    combined = record_repr + page_repr + result_repr
    assert "secret-practice" not in combined
    assert "SYN-RC" not in combined
    assert "mock-cursor" not in combined
    assert "record_count=4" in result_repr


def test_practice_context_is_server_owned_and_payload_practice_is_rejected():
    parsed = parse_normalized_page(
        _payload(),
        server_practice_id="server-practice",
    )
    assert parsed.records[0].target_practice_id == "server-practice"

    injected = _payload()
    injected["records"][0]["practice_id"] = "payload-selected-practice"
    with pytest.raises(AthenaUnknownFieldError):
        parse_normalized_page(
            injected,
            server_practice_id="server-practice",
        )


@pytest.mark.parametrize("missing", ["records", "next_cursor", "checkpoint"])
def test_partial_page_fails_closed(missing):
    value = _payload()
    del value[missing]
    with pytest.raises(AthenaPartialResponseError):
        parse_normalized_page(value, server_practice_id="practice-a")


def test_unknown_top_level_and_visit_fields_fail_closed():
    top_level = _payload()
    top_level["future_vendor_field"] = "ignored-by-unsafe-client"
    with pytest.raises(AthenaUnknownFieldError):
        parse_normalized_page(top_level, server_practice_id="practice-a")

    visit = _payload()
    visit["records"][0]["visit"]["unknown_clinical_value"] = "x"
    with pytest.raises(AthenaUnknownFieldError):
        parse_normalized_page(visit, server_practice_id="practice-a")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version="vendor-v2"),
        lambda value: value.update(records="not-a-list"),
        lambda value: value["records"][0]["visit"].update(patient_age=True),
        lambda value: value["records"][0]["visit"].update(patient_age=126),
        lambda value: value["records"][0]["visit"].update(
            signed_note="x" * 131_073
        ),
        lambda value: value["records"][0]["visit"].update(
            submitted_icd10=["I10"] * 51
        ),
    ],
)
def test_invalid_or_unbounded_schema_fails_without_echo(mutate):
    value = _payload()
    mutate(value)
    with pytest.raises(AthenaSchemaError) as caught:
        parse_normalized_page(value, server_practice_id="practice-a")
    assert str(caught.value) == "ATHENA_RESPONSE_SCHEMA_INVALID"
    assert "signed_note" not in str(caught.value)


def test_page_record_limit_is_enforced():
    value = _payload([_valid_record()] * (MAX_PAGE_RECORDS + 1))
    with pytest.raises(AthenaSchemaError):
        parse_normalized_page(value, server_practice_id="practice-a")


def test_exact_duplicate_replay_is_counted_once():
    first = parse_normalized_page(
        _payload(next_cursor="page-2"),
        server_practice_id="practice-a",
    )
    second = parse_normalized_page(
        _payload(checkpoint="checkpoint-2"),
        server_practice_id="practice-a",
    )

    class Pages:
        mode = "test"

        def read_page(self, cursor=None):
            return first if cursor is None else second

    result = read_all_pages(Pages())
    assert len(result.records) == 1
    assert result.replay_count == 1
    assert result.page_count == 2


def test_same_idempotency_identifier_with_changed_payload_fails_closed():
    first = parse_normalized_page(
        _payload(next_cursor="page-2"),
        server_practice_id="practice-a",
    )
    changed = _valid_record()
    changed["visit"]["signed_note"] += " changed"
    second = parse_normalized_page(
        _payload([changed], checkpoint="checkpoint-2"),
        server_practice_id="practice-a",
    )

    class Pages:
        mode = "test"

        def read_page(self, cursor=None):
            return first if cursor is None else second

    with pytest.raises(AthenaSchemaError):
        read_all_pages(Pages())


def test_cursor_loop_and_page_budget_fail_closed():
    loop = parse_normalized_page(
        _payload([], next_cursor="same"),
        server_practice_id="practice-a",
    )

    class Loop:
        mode = "test"

        def read_page(self, cursor=None):
            return loop

    with pytest.raises(AthenaSchemaError):
        read_all_pages(Loop())

    class Endless:
        mode = "test"
        calls = 0

        def read_page(self, cursor=None):
            self.calls += 1
            return parse_normalized_page(
                _payload(
                    [],
                    next_cursor=f"page-{self.calls}",
                    checkpoint=f"checkpoint-{self.calls}",
                ),
                server_practice_id="practice-a",
            )

    with pytest.raises(AthenaSchemaError):
        read_all_pages(Endless(), max_pages=2)


def test_http_success_is_bounded_and_uses_injected_bearer():
    captured = {}

    def urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["method"] = request.method
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response({"ok": True})

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        urlopen=urlopen,
        sleep=lambda _: None,
    )
    result = transport.request_json(
        BASE_URL + "/visits",
        method="POST",
        body={"cursor": "synthetic-cursor"},
    )

    assert result == {"ok": True}
    assert captured == {
        "authorization": "Bearer synthetic-token",
        "method": "POST",
        "body": {"cursor": "synthetic-cursor"},
        "timeout": 30.0,
    }


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_terminal_and_sanitized(status):
    secret = "credential-that-must-not-escape"
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(request.full_url, status, secret, {}, None)

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: secret,
        urlopen=urlopen,
        sleep=lambda _: None,
    )
    with pytest.raises(AthenaAuthenticationError) as caught:
        transport.request_json(BASE_URL + "/visits")
    assert calls == 1
    assert str(caught.value) == "ATHENA_AUTHENTICATION_FAILED"
    assert secret not in str(caught.value)


def test_timeout_retries_are_bounded_and_report_only_static_code():
    calls = 0
    sleeps = []

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise socket.timeout("vendor-record-should-not-escape")

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        max_retries=2,
        urlopen=urlopen,
        sleep=sleeps.append,
    )
    with pytest.raises(AthenaTimeoutError) as caught:
        transport.request_json(BASE_URL + "/visits")
    assert calls == 3
    assert sleeps == [1, 2]
    assert str(caught.value) == "ATHENA_REQUEST_TIMEOUT"
    assert "vendor-record" not in str(caught.value)


def test_retry_exhaustion_for_network_failure_is_bounded():
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("raw-vendor-error")

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        max_retries=1,
        urlopen=urlopen,
        sleep=lambda _: None,
    )
    with pytest.raises(AthenaRetryExhausted) as caught:
        transport.request_json(BASE_URL + "/visits")
    assert calls == 2
    assert str(caught.value) == "ATHENA_RETRY_EXHAUSTED"


def test_retryable_server_error_can_recover_but_rejection_is_terminal():
    calls = 0

    def recovering(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "raw", {}, None)
        return _Response({"ok": True})

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        urlopen=recovering,
        sleep=lambda _: None,
    )
    assert transport.request_json(BASE_URL + "/visits") == {"ok": True}
    assert calls == 2

    def rejected(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "raw", {}, None)

    rejected_transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        urlopen=rejected,
    )
    with pytest.raises(AthenaRequestRejected):
        rejected_transport.request_json(BASE_URL + "/visits")


def test_default_transport_refuses_redirect_without_forwarding_token(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.get_header("Authorization")))
            raise urllib.error.HTTPError(
                request.full_url,
                302,
                "redirect refused",
                {"Location": "https://attacker.invalid/steal"},
                None,
            )

    def build_opener(*handlers):
        assert any(
            type(handler).__name__ == "_NoRedirectHandler"
            for handler in handlers
        )
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        sleep=lambda _: None,
    )
    with pytest.raises(AthenaRequestRejected):
        transport.request_json(BASE_URL + "/visits")

    assert calls == [(BASE_URL + "/visits", "Bearer synthetic-token")]


@pytest.mark.parametrize(
    "payload,headers",
    [
        (b"not-json", {}),
        (["not", "an", "object"], {}),
        ({"ok": True}, {"Content-Length": "9999999"}),
        ({"ok": True}, {"Content-Length": "not-an-integer"}),
    ],
)
def test_invalid_or_oversized_http_response_is_schema_failure(payload, headers):
    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        urlopen=lambda request, timeout: _Response(payload, headers=headers),
    )
    with pytest.raises(AthenaSchemaError):
        transport.request_json(BASE_URL + "/visits")


def test_token_provider_and_target_validation_fail_without_network():
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return _Response({})

    failed_token = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: (_ for _ in ()).throw(RuntimeError("secret")),
        urlopen=urlopen,
    )
    with pytest.raises(AthenaAuthenticationError):
        failed_token.request_json(BASE_URL + "/visits")

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        urlopen=urlopen,
    )
    with pytest.raises(AthenaSchemaError):
        transport.request_json("https://attacker.example/visits")
    assert calls == 0


def test_live_composition_requires_explicit_contract_and_preserves_server_scope():
    expected_payload = _payload([])

    class Contract:
        def build_page_request(self, cursor):
            assert cursor is None
            return AthenaRequest(BASE_URL + "/reviewed-contract/visits")

        def decode_page(self, payload, *, server_practice_id):
            assert payload == expected_payload
            return parse_normalized_page(
                payload,
                server_practice_id=server_practice_id,
            )

    transport = AthenaHTTPTransport(
        base_url=BASE_URL,
        token_provider=lambda: "synthetic-token",
        urlopen=lambda request, timeout: _Response(expected_payload),
    )
    client = AthenaSandboxClient(
        transport=transport,
        contract=Contract(),
        server_practice_id="server-practice",
    )
    page = client.read_page()
    assert page.records == ()
