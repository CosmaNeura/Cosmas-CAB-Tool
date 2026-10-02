"""Offline tests of the executable HTTPS protocol; no real credentials/socket."""
import base64
import io
import json
import time
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from cosmaneura_billing.athena_client import (
    ATHENA_PREVIEW_ORIGIN, ATHENA_PREVIEW_SCOPE, ATHENA_PREVIEW_TOKEN_URL,
    AthenaAccessToken, AthenaClientError, AthenaStaffReadClient, MockAthenaStaffReadClient,
)


def wire(value, url, *, status=200, headers=None):
    response = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())
    response.status = status
    response.headers = headers or {"Content-Type": "application/json"}
    response.geturl = lambda: url
    return response


def client(responses, calls=None, **kwargs):
    calls = calls if calls is not None else []
    def open_mock(request, timeout):
        calls.append(request)
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value(request, timeout) if callable(value) else wire(value, request.full_url)
    return AthenaStaffReadClient(token_provider=lambda: AthenaAccessToken("fake", ATHENA_PREVIEW_SCOPE, time.time() + 300),
                                 urlopen=open_mock, **kwargs)


def patient():
    return [{"patientid": "60178", "firstname": "Synthetic", "mobilephone": "+12025550142"}]


def policies(n=1):
    return [{"insurancepackageid": str(40+i), "insuranceid": str(70+i)} for i in range(n)]


def continuation(offset=5):
    return f"/v1/195900/patients/60178/insurances?limit=5&offset={offset}&showcancelled=true&showfullssn=false"


def test_actual_oauth_and_get_wire_contract_no_environment_or_activation(monkeypatch):
    calls = []
    def sender(request, timeout):
        calls.append(request)
        assert 0 < timeout <= 5
        value = ({"access_token": "synthetic-token", "token_type": "Bearer", "expires_in": "300"}
                 if request.full_url == ATHENA_PREVIEW_TOKEN_URL else
                 {"insurances": [], "totalcount": 0} if "/insurances?" in request.full_url else patient())
        return wire(value, request.full_url)
    monkeypatch.setenv("COSMANEURA_ATHENA_CLIENT_SECRET", "must-not-be-read")
    c = AthenaStaffReadClient(client_id="synthetic-id", client_secret="synthetic-secret", urlopen=sender)
    assert calls == [] and "synthetic-secret" not in repr(c)
    assert c.read_card("60178")["insuranceState"] == "empty"
    assert [r.get_method() for r in calls] == ["POST", "GET", "GET"]
    assert calls[0].headers["Authorization"] == "Basic " + base64.b64encode(b"synthetic-id:synthetic-secret").decode()
    assert parse_qs(calls[0].data.decode()) == {"grant_type": ["client_credentials"], "scope": [ATHENA_PREVIEW_SCOPE]}
    assert calls[1].full_url == ATHENA_PREVIEW_ORIGIN + "/v1/195900/patients/60178?showfullssn=false"
    c.read_card("60178")
    assert len([r for r in calls if r.get_method() == "POST"]) == 1


@pytest.mark.parametrize("patient_id", [None, [], 60178, "1", "60185", "60178?patientfacingcall=true", "../60178"])
def test_invalid_patient_never_calls_token_or_transport(patient_id):
    c = AthenaStaffReadClient(token_provider=lambda: pytest.fail("token action"), urlopen=lambda *a, **k: pytest.fail("network"))
    with pytest.raises(AthenaClientError):
        c.read_card(patient_id)


@pytest.mark.parametrize("token", [
    {"access_token": "token", "expires_in": 300},
    {"access_token": "token", "token_type": "Bearer", "expires_in": "0300"},
    {"access_token": "token", "token_type": "Bearer", "expires_in": 3601},
    {"access_token": "token", "token_type": "Bearer", "expires_in": 300, "scope": ATHENA_PREVIEW_SCOPE + " extra"},
])
def test_bad_oauth_metadata_stops_before_get(token):
    requests = []
    def sender(request, timeout):
        requests.append(request)
        return wire(token, request.full_url)
    c = AthenaStaffReadClient(client_id="fake", client_secret="fake", urlopen=sender)
    with pytest.raises(AthenaClientError):
        c.read_card("60178")
    assert len(requests) == 1


def test_bounded_pagination_retains_original_pages_and_partial_failure():
    page = {"insurances": policies(5), "totalcount": 6, "next": continuation()}
    calls = []
    c = client([patient(), page, {"insurances": policies(), "totalcount": 6}], calls)
    result = c.read_card("60178")
    assert result["insuranceState"] == "complete" and len(result["insurancePages"]) == 2
    assert parse_qs(urlsplit(calls[-1].full_url).query)["offset"] == ["5"]
    c = client([patient(), page, TimeoutError("must not reflect secret")])
    partial = c.read_card("60178")
    assert partial["insuranceState"] == "partial" and len(partial["insurancePages"]) == 1
    assert "secret" not in json.dumps({k:v for k,v in partial.items() if k not in ("patient","insurancePages")})


@pytest.mark.parametrize("link", [
    continuation().replace("showcancelled=true", "SHOWCANCELLED=True").replace("showfullssn=false", "SHOWFULLSSN=False"),
    continuation().replace("showcancelled=true", "ShowCancelled=TRUE").replace("showfullssn=false", "ShowFullSsn=FALSE"),
])
def test_preview_flag_casing_keeps_original_page_and_uses_canonical_next_get(link):
    page = {"insurances": policies(5), "totalcount": 6, "next": link}
    calls = []
    result = client([patient(), page, {"insurances": policies(), "totalcount": 6}], calls).read_card("60178")
    assert result["insuranceState"] == "complete"
    assert json.loads(result["insurancePages"][0])["next"] == link
    assert calls[-1].full_url == ATHENA_PREVIEW_ORIGIN + continuation()


def test_final_continuation_can_advertise_exact_remaining_record_count():
    first = {"insurances": policies(5), "totalcount": 12, "next": continuation()}
    last_link = continuation(10).replace("limit=5", "limit=2")
    second = {"insurances": policies(5), "totalcount": 12, "next": last_link}
    calls = []
    result = client([patient(), first, second, {"insurances": policies(2), "totalcount": 12}], calls).read_card("60178")
    assert result["insuranceState"] == "complete" and len(result["insurancePages"]) == 3
    assert json.loads(result["insurancePages"][1])["next"] == last_link
    assert calls[-1].full_url == ATHENA_PREVIEW_ORIGIN + continuation(10)


@pytest.mark.parametrize("limit", ["0", "1", "3", "6", "02"])
def test_final_continuation_limit_must_be_cap_or_exact_remainder(limit):
    first = {"insurances": policies(5), "totalcount": 12, "next": continuation()}
    second = {"insurances": policies(5), "totalcount": 12,
              "next": continuation(10).replace("limit=5", "limit=" + limit)}
    calls = []
    result = client([patient(), first, second], calls).read_card("60178")
    assert result["insuranceState"] == "partial" and len(result["insurancePages"]) == 1
    assert len(calls) == 3


@pytest.mark.parametrize("next_link", ["https://evil.invalid/a", "/v1/999/patients/60178/insurances",
    continuation(0), continuation() + "&PATIENTFACINGCALL=true", continuation() + "&offset=5",
    continuation() + "&OFFSET=5", continuation() + "&SHOWFULLSSN=false",
    continuation().replace("showfullssn=false", "SHOWFULLSSN=True"),
    continuation().replace("showcancelled=true", "SHOWCANCELLED=False"),
    continuation().replace("offset=5", "OFFSET=05"),
    continuation().replace("showfullssn=false", "SHOWFULLSSN=0")])
def test_continuation_never_escapes_exact_manifest(next_link):
    calls = []
    c = client([patient(), {"insurances": policies(5), "totalcount": 6, "next": next_link}], calls)
    assert c.read_card("60178")["insuranceState"] == "unavailable"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_status_failures_are_static_without_automatic_retry(status):
    calls = []
    failure = urllib.error.HTTPError("https://secret", status, "patient-secret", {}, None)
    c = client([failure], calls)
    with pytest.raises(AthenaClientError) as error:
        c.read_card("60178")
    assert "secret" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize("raw", [b'{"patientid":"60178","patientid":"other"}', b'[NaN]', b'['*1200,
                                 b'"' + b'x'*262144 + b'"'])
def test_bad_or_unbounded_payload_fails_closed(raw):
    with pytest.raises(AthenaClientError):
        client([raw]).read_card("60178")


@pytest.mark.parametrize("headers", [{"Content-Type":"text/html"}, {"Content-Type":"application/json","Content-Encoding":"gzip"},
    {"Content-Type":"application/json","Content-Length":"9999999"}])
def test_encoding_and_media_caps(headers):
    with pytest.raises(AthenaClientError):
        client([lambda r,t: wire(patient(), r.full_url, headers=headers)]).read_card("60178")


def test_no_redirect_and_total_deadline_enforced():
    with pytest.raises(AthenaClientError):
        client([lambda r,t: wire(patient(), "https://wrong.invalid")]).read_card("60178")
    moments = iter([0, 26])
    with pytest.raises(AthenaClientError):
        client([], monotonic=lambda: next(moments)).read_card("60178")


def test_billing_linkage_summary_transform_and_torn_source():
    row = {"patientid":"60178", "encounterid":"88001", "departmentid":"1", "lastupdated":"09/29/2026"}
    page = {"encounters":[row], "totalcount":1}
    summary = {"summaryhtml":"<p>Reviewed &amp; documented</p><script>secret()</script><p>Original units 5 mg.</p>"}
    c = client([page, row, summary, row])
    result = c.read_billing("60178","1","88001")
    assert result["readyForHeader"] is False and "place_of_service" in result["unknownFields"]
    assert "secret()" not in result["note"]["plainText"] and "5 mg" in result["note"]["plainText"]
    assert result["note"]["signatureInterpretation"] == "unverified"
    with pytest.raises(AthenaClientError):
        client([page,row,summary,{**row,"lastupdated":"changed"}]).read_billing("60178","1","88001")
    with pytest.raises(AthenaClientError):
        client([page]).read_billing("60178","1","999")
    assert MockAthenaStaffReadClient().read_billing("60178","1")["encounters"][0]["encounterid"]["value"] == "88001"


def test_integer_response_identifiers_preserve_binding_and_canonical_note_request():
    row = {"encounterid": 88001, "patientid": 60178, "departmentid": 1,
           "practiceid": 195900, "appointmentid": 73001, "providerid": 42001}
    page = {"encounters": [row], "totalcount": 1}
    summary = {"patientid": 60178, "encounterid": 88001, "summaryhtml": "<p>Synthetic note.</p>"}
    calls = []
    result = client([page, row, summary, row], calls).read_billing("60178", "1", "88001")
    for key in ("encounterid", "departmentid", "appointmentid", "providerid"):
        assert result["encounters"][0][key] == {"state": "present", "value": str(row[key])}
    assert result["note"]["encounterId"] == "88001"
    assert result["note"]["metadata"]["encounterid"]["value"] == "88001"
    assert calls[1].full_url.endswith("/chart/encounter/88001")
    assert calls[2].full_url.endswith("/chart/encounters/88001/summary")
    assert type(row["encounterid"]) is int


def test_exact_encounter_metadata_accepts_single_record_array_without_losing_provenance():
    row = {"encounterid": 88001, "departmentid": 1}
    page = {"encounters": [row], "totalcount": 1}
    summary = {"summaryhtml": "<p>Synthetic note.</p>"}
    result = client([page, [row], summary, [row]]).read_billing("60178", "1", "88001")
    import hashlib
    assert result["note"]["metadataSha256"] == hashlib.sha256(json.dumps([row]).encode()).hexdigest()
    assert result["note"]["metadata"]["encounterid"]["value"] == "88001"


@pytest.mark.parametrize("metadata", [[], [None], [{"encounterid": 88002, "departmentid": 1}],
                                      [{"encounterid": 88001}, {"encounterid": 88001}]])
def test_exact_encounter_metadata_rejects_missing_ambiguous_or_mismatched_record(metadata):
    calls = []
    with pytest.raises(AthenaClientError):
        client([{"encounters": [{"encounterid": 88001, "departmentid": 1}], "totalcount": 1},
                metadata], calls).read_billing("60178", "1", "88001")
    assert len(calls) == 2


@pytest.mark.parametrize("identifier", [True, 88001.0, 0, -1, 10**18, "088001", "88001 "])
def test_response_id_coercion_never_accepts_ambiguous_numbers(identifier):
    page = {"encounters": [{"encounterid": identifier, "departmentid": 1}], "totalcount": 1}
    calls = []
    result = client([page], calls).read_billing("60178", "1")
    assert result["state"] == "unavailable" and result["encounters"] == []
    assert len(calls) == 1


def test_mixed_wire_types_do_not_conceal_duplicate_encounters_or_wrong_patient():
    rows = [{"encounterid": 88001, "departmentid": 1}, {"encounterid": "88001", "departmentid": "1"}]
    with pytest.raises(AthenaClientError):
        client([{"encounters": rows, "totalcount": 2}]).read_billing("60178", "1")
    row = {"encounterid": 88001, "departmentid": 1, "patientid": 60179}
    result = client([{"encounters": [row], "totalcount": 1}]).read_billing("60178", "1")
    assert result["state"] == "unavailable"
    row["patientid"] = 60178
    with pytest.raises(AthenaClientError):
        client([{"encounters": [row], "totalcount": 1}, row,
                {"patientid": 60179, "summaryhtml": "<p>Wrong source.</p>"}]).read_billing("60178", "1", "88001")


@pytest.mark.parametrize("value", [False, 0, [], {}])
@pytest.mark.parametrize("field", ["next", "previous"])
def test_falsey_nonstring_continuations_never_mean_complete(value,field):
    page={"encounters":[{"encounterid":"88001","departmentid":"1"}],"totalcount":1,field:value}
    result=client([page]).read_billing("60178","1")
    assert result["state"] == "unavailable" and result["encounters"] == []
