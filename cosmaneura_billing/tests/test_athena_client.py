"""Mock-first Athena boundary: parsing, pagination, and HTTP hardening."""
from __future__ import annotations

import json
import socket
import urllib.error
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType

import pytest

import cosmaneura_billing.athena_client as athena_client
from cosmaneura_billing.athena_client import (
    ATHENA_PREVIEW_INTROSPECTION_URL,
    ATHENA_PREVIEW_ORIGIN,
    ATHENA_PREVIEW_PRACTICE_ID,
    ATHENA_PREVIEW_SCOPE,
    ATHENA_PREVIEW_TOKEN_URL,
    MAX_PAGE_RECORDS,
    AthenaAccessToken,
    AthenaAuthenticationError,
    AthenaHTTPTransport,
    AthenaOperationUnavailable,
    AthenaPartialResponseError,
    AthenaPreviewOperation,
    AthenaPreviewOperationContract,
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
    validate_athena_token_introspection,
    validate_athena_token_response,
)
from cosmaneura_billing.corpus.demo_release_candidate import (
    load_demo_release_candidate,
)


NOW = 1_800_000_000


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


@pytest.fixture
def confirmed_preview_operation(monkeypatch):
    """Install a test-only spec; the production allowlist stays empty."""

    spec = athena_client._AthenaPreviewOperationSpec(
        path_template="/unit-fixture/{vendor_practice_id}/records",
        query_keys=frozenset({"page"}),
    )
    monkeypatch.setattr(
        athena_client,
        "_ATHENA_PREVIEW_OPERATION_ALLOWLIST",
        MappingProxyType({
            AthenaPreviewOperation.PRACTICE_REFERENCE: spec,
        }),
    )
    return AthenaPreviewOperationContract()


def _access_token(
    *,
    token: str = "synthetic-token",
    expires_at: float = NOW + 300,
) -> AthenaAccessToken:
    return AthenaAccessToken(
        access_token=token,
        scope=ATHENA_PREVIEW_SCOPE,
        expires_at=expires_at,
    )


def _preview_request(
    contract: AthenaPreviewOperationContract,
    *,
    page: str | None = None,
) -> AthenaRequest:
    query = None if page is None else {"page": page}
    return contract.build_request(
        AthenaPreviewOperation.PRACTICE_REFERENCE,
        query=query,
    )


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


def test_production_operation_allowlist_is_empty_and_fail_closed():
    contract = AthenaPreviewOperationContract()

    with pytest.raises(AthenaOperationUnavailable) as caught:
        contract.build_request(AthenaPreviewOperation.CLAIMS)

    assert str(caught.value) == "ATHENA_OPERATION_UNAVAILABLE"
    assert athena_client._ATHENA_PREVIEW_OPERATION_ALLOWLIST == {}


def test_verified_preview_metadata_is_exactly_pinned():
    assert ATHENA_PREVIEW_ORIGIN == (
        "https://api.preview.platform.athenahealth.com"
    )
    assert ATHENA_PREVIEW_TOKEN_URL == (
        "https://api.preview.platform.athenahealth.com/oauth2/v1/token"
    )
    assert ATHENA_PREVIEW_INTROSPECTION_URL == (
        "https://api.preview.platform.athenahealth.com/oauth2/v1/introspect"
    )
    assert ATHENA_PREVIEW_PRACTICE_ID == "195900"
    assert ATHENA_PREVIEW_SCOPE == "athena/service/Athenanet.MDP.*"


def test_named_contract_binds_fixed_vendor_practice_and_canonical_query(
    confirmed_preview_operation,
):
    request = _preview_request(confirmed_preview_operation, page="next-page")

    assert confirmed_preview_operation.vendor_practice_id == (
        ATHENA_PREVIEW_PRACTICE_ID
    )
    assert request.method == "GET"
    assert request.url == (
        ATHENA_PREVIEW_ORIGIN
        + "/unit-fixture/195900/records?page=next-page"
    )
    assert request.query == (("page", "next-page"),)

    with pytest.raises(AthenaSchemaError):
        AthenaPreviewOperationContract(vendor_practice_id="195901")
    with pytest.raises(AthenaSchemaError):
        confirmed_preview_operation.build_request(
            AthenaPreviewOperation.PRACTICE_REFERENCE,
            query={"unreviewed": "value"},
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: replace(request, method="POST"),
        lambda request: replace(request, method="PUT"),
        lambda request: replace(request, url=request.url + "/../claims"),
        lambda request: replace(request, url=request.url + "/%2e%2e/claims"),
        lambda request: replace(
            request,
            url="https://attacker.invalid/unit-fixture/195900/records",
        ),
        lambda request: replace(request, query=(("unknown", "value"),)),
    ],
)
def test_request_validation_precedes_token_and_network(
    confirmed_preview_operation,
    mutate,
):
    token_calls = 0
    network_calls = 0

    def token_provider():
        nonlocal token_calls
        token_calls += 1
        return _access_token()

    def urlopen(request, timeout):
        nonlocal network_calls
        network_calls += 1
        return _Response({})

    transport = AthenaHTTPTransport(
        token_provider=token_provider,
        operation_contract=confirmed_preview_operation,
        urlopen=urlopen,
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaSchemaError):
        transport.request_json(mutate(_preview_request(
            confirmed_preview_operation,
        )))

    assert token_calls == 0
    assert network_calls == 0


def test_http_success_is_get_only_bodyless_and_uses_validated_bearer(
    confirmed_preview_operation,
):
    captured = {}

    def urlopen(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["method"] = request.method
        captured["body"] = request.data
        captured["timeout"] = timeout
        return _Response({"ok": True})

    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        urlopen=urlopen,
        sleep=lambda _: None,
        clock=lambda: NOW,
    )
    result = transport.request_json(_preview_request(
        confirmed_preview_operation,
        page="synthetic-cursor",
    ))

    assert result == {"ok": True}
    assert captured == {
        "authorization": "Bearer synthetic-token",
        "method": "GET",
        "body": None,
        "timeout": 30.0,
    }


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_is_terminal_and_sanitized(
    confirmed_preview_operation,
    status,
):
    secret = "credential-that-must-not-escape"
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(request.full_url, status, secret, {}, None)

    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(token=secret),
        operation_contract=confirmed_preview_operation,
        urlopen=urlopen,
        sleep=lambda _: None,
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaAuthenticationError) as caught:
        transport.request_json(_preview_request(confirmed_preview_operation))
    assert calls == 1
    assert str(caught.value) == "ATHENA_AUTHENTICATION_FAILED"
    assert secret not in str(caught.value)


def test_timeout_retries_are_bounded_and_report_only_static_code(
    confirmed_preview_operation,
):
    calls = 0
    sleeps = []

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise socket.timeout("vendor-record-should-not-escape")

    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        max_retries=2,
        urlopen=urlopen,
        sleep=sleeps.append,
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaTimeoutError) as caught:
        transport.request_json(_preview_request(confirmed_preview_operation))
    assert calls == 3
    assert sleeps == [1, 2]
    assert str(caught.value) == "ATHENA_REQUEST_TIMEOUT"
    assert "vendor-record" not in str(caught.value)


def test_retry_exhaustion_for_network_failure_is_bounded(
    confirmed_preview_operation,
):
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("raw-vendor-error")

    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        max_retries=1,
        urlopen=urlopen,
        sleep=lambda _: None,
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaRetryExhausted) as caught:
        transport.request_json(_preview_request(confirmed_preview_operation))
    assert calls == 2
    assert str(caught.value) == "ATHENA_RETRY_EXHAUSTED"


def test_retryable_server_error_can_recover_but_rejection_is_terminal(
    confirmed_preview_operation,
):
    calls = 0
    request = _preview_request(confirmed_preview_operation)

    def recovering(raw_request, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                raw_request.full_url,
                503,
                "raw",
                {},
                None,
            )
        return _Response({"ok": True})

    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        urlopen=recovering,
        sleep=lambda _: None,
        clock=lambda: NOW,
    )
    assert transport.request_json(request) == {"ok": True}
    assert calls == 2

    def rejected(raw_request, timeout):
        raise urllib.error.HTTPError(
            raw_request.full_url,
            400,
            "raw",
            {},
            None,
        )

    rejected_transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        urlopen=rejected,
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaRequestRejected):
        rejected_transport.request_json(request)


def test_retry_revalidates_token_after_backoff(
    confirmed_preview_operation,
):
    current = [float(NOW)]
    network_calls = 0

    def retryable(request, timeout):
        nonlocal network_calls
        network_calls += 1
        raise urllib.error.HTTPError(request.full_url, 503, "raw", {}, None)

    def advance(seconds):
        current[0] += seconds

    token = _access_token(expires_at=NOW + 0.5)
    transport = AthenaHTTPTransport(
        token_provider=lambda: token,
        operation_contract=confirmed_preview_operation,
        max_retries=1,
        urlopen=retryable,
        sleep=advance,
        clock=lambda: current[0],
    )

    with pytest.raises(AthenaAuthenticationError):
        transport.request_json(_preview_request(confirmed_preview_operation))
    assert network_calls == 1


def test_default_transport_refuses_redirect_without_forwarding_again(
    monkeypatch,
    confirmed_preview_operation,
):
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
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        sleep=lambda _: None,
        clock=lambda: NOW,
    )
    request = _preview_request(confirmed_preview_operation)
    with pytest.raises(AthenaRequestRejected):
        transport.request_json(request)

    assert calls == [(request.url, "Bearer synthetic-token")]


@pytest.mark.parametrize(
    "payload,headers",
    [
        (b"not-json", {}),
        (["not", "an", "object"], {}),
        ({"ok": True}, {"Content-Length": "9999999"}),
        ({"ok": True}, {"Content-Length": "not-an-integer"}),
    ],
)
def test_invalid_or_oversized_http_response_is_schema_failure(
    confirmed_preview_operation,
    payload,
    headers,
):
    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        urlopen=lambda request, timeout: _Response(payload, headers=headers),
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaSchemaError):
        transport.request_json(_preview_request(confirmed_preview_operation))


def test_invalid_token_provider_fails_without_network(
    confirmed_preview_operation,
):
    calls = 0

    def urlopen(request, timeout):
        nonlocal calls
        calls += 1
        return _Response({})

    failed_token = AthenaHTTPTransport(
        token_provider=lambda: (_ for _ in ()).throw(RuntimeError("secret")),
        operation_contract=confirmed_preview_operation,
        urlopen=urlopen,
        clock=lambda: NOW,
    )
    with pytest.raises(AthenaAuthenticationError):
        failed_token.request_json(_preview_request(confirmed_preview_operation))

    invalid_tokens = (
        _access_token(expires_at=NOW),
        _access_token(expires_at=NOW + 3_601),
        "unvalidated-raw-token",
        AthenaAccessToken(
            access_token="synthetic-token",
            scope="extra.scope",
            expires_at=NOW + 300,
        ),
    )
    for invalid_token in invalid_tokens:
        invalid_transport = AthenaHTTPTransport(
            token_provider=lambda value=invalid_token: value,
            operation_contract=confirmed_preview_operation,
            urlopen=urlopen,
            clock=lambda: NOW,
        )
        with pytest.raises(AthenaAuthenticationError):
            invalid_transport.request_json(_preview_request(
                confirmed_preview_operation,
            ))
    assert calls == 0


def test_token_response_requires_exact_scope_and_bounded_future_expiry():
    payload = {
        "access_token": "credential-that-must-not-escape",
        "token_type": "Bearer",
        "expires_in": 300,
        "scope": ATHENA_PREVIEW_SCOPE,
    }
    token = validate_athena_token_response(
        payload,
        requested_scope=ATHENA_PREVIEW_SCOPE,
        now=NOW,
    )
    assert token.scope == ATHENA_PREVIEW_SCOPE
    assert token.expires_at == NOW + 300
    assert "credential-that-must-not-escape" not in repr(token)

    for invalid in (
        {**payload, "scope": ATHENA_PREVIEW_SCOPE + " extra.scope"},
        {key: value for key, value in payload.items() if key != "scope"},
        {**payload, "expires_in": 0},
        {**payload, "expires_in": 3_601},
        {**payload, "token_type": "Basic"},
    ):
        with pytest.raises(AthenaAuthenticationError):
            validate_athena_token_response(
                invalid,
                requested_scope=ATHENA_PREVIEW_SCOPE,
                now=NOW,
            )


def test_introspection_requires_active_exact_scope_expiry_and_optional_client():
    payload = {
        "active": True,
        "scope": ATHENA_PREVIEW_SCOPE,
        "exp": NOW + 300,
        "client_id": "synthetic-client-id",
        # Accepted but deliberately not interpreted before vendor confirmation.
        "aud": "unreviewed-audience",
        "iss": "unreviewed-issuer",
    }
    result = validate_athena_token_introspection(
        payload,
        requested_scope=ATHENA_PREVIEW_SCOPE,
        expected_client_id="synthetic-client-id",
        now=NOW,
    )
    assert result.active is True
    assert result.client_id == "synthetic-client-id"
    assert "unreviewed" not in repr(result)

    without_client = dict(payload)
    del without_client["client_id"]
    assert validate_athena_token_introspection(
        without_client,
        requested_scope=ATHENA_PREVIEW_SCOPE,
        expected_client_id="synthetic-client-id",
        now=NOW,
    ).client_id is None

    invalid_payloads = (
        {**payload, "active": False},
        {**payload, "scope": ATHENA_PREVIEW_SCOPE + " extra.scope"},
        {key: value for key, value in payload.items() if key != "scope"},
        {**payload, "exp": NOW},
        {**payload, "exp": NOW + 3_601},
        {**payload, "client_id": "wrong-client"},
    )
    for invalid in invalid_payloads:
        with pytest.raises(AthenaAuthenticationError):
            validate_athena_token_introspection(
                invalid,
                requested_scope=ATHENA_PREVIEW_SCOPE,
                expected_client_id="synthetic-client-id",
                now=NOW,
            )


def test_live_composition_uses_named_operation_and_preserves_server_scope(
    confirmed_preview_operation,
):
    expected_payload = _payload([])

    class Contract:
        operation = AthenaPreviewOperation.PRACTICE_REFERENCE

        def build_page_query(self, cursor):
            return None if cursor is None else {"page": cursor}

        def decode_page(self, payload, *, server_practice_id):
            assert payload == expected_payload
            return parse_normalized_page(
                payload,
                server_practice_id=server_practice_id,
            )

    transport = AthenaHTTPTransport(
        token_provider=lambda: _access_token(),
        operation_contract=confirmed_preview_operation,
        urlopen=lambda request, timeout: _Response(expected_payload),
        clock=lambda: NOW,
    )
    client = AthenaSandboxClient(
        transport=transport,
        operation_contract=confirmed_preview_operation,
        contract=Contract(),
        server_practice_id="server-practice",
    )
    page = client.read_page()
    assert page.records == ()
