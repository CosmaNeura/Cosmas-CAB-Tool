"""Chart query context never substitutes for an encounter's source department."""
from copy import deepcopy
import hashlib
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from cosmaneura_billing.athena_client import AthenaClientError
from test_athena_staff_client import client, wire


def row(department="150", *, encounter="88001"):
    value = {"encounterid":encounter,"patientid":"60178","practiceid":"195900"}
    if department != "absent": value["departmentid"] = department
    return value


def page(rows): return {"encounters":rows,"totalcount":len(rows)}
SUMMARY = {"summaryhtml":"<p>Explicitly authored synthetic source note.</p>"}


def test_mixed_department_page_keeps_query_and_raw_page_identity():
    rows = [row(150,encounter=str(63892-i)) for i in range(3)] + [row(1,encounter=str(61984-i)) for i in range(2)]
    original = deepcopy(rows); calls = []
    result = client([page(rows)],calls).read_billing("60178","1")
    assert result["state"] == "complete" and len(result["encounters"]) == 5
    assert result["departmentId"] == "1" and result["departmentIdMeaning"] == "chart_query"
    assert [r["departmentid"]["value"] for r in result["encounters"]] == ["150"]*3 + ["1"]*2
    assert result["pageHashes"] == [hashlib.sha256(json.dumps(page(rows)).encode()).hexdigest()]
    assert parse_qs(urlsplit(calls[0].full_url).query) == {"departmentid":["1"],"limit":["5"],"offset":["0"]}
    assert rows == original


@pytest.mark.parametrize("department",[150,"150",999999999999999999,"999999999999999999"])
def test_selected_source_department_binds_metadata_not_query(department):
    discovered, metadata = row(department),row(str(department)); calls = []
    result = client([page([discovered]),metadata,SUMMARY,metadata],calls).read_billing("60178","1","88001")
    assert result["note"]["sourceDepartmentId"] == str(department)
    assert result["note"]["sourceDepartmentOrigin"] == "list_and_metadata"
    assert result["note"]["metadataSha256"] == hashlib.sha256(json.dumps(metadata).encode()).hexdigest()
    assert [urlsplit(r.full_url).path for r in calls] == ["/v1/195900/chart/60178/encounters",
        "/v1/195900/chart/encounter/88001","/v1/195900/chart/encounters/88001/summary","/v1/195900/chart/encounter/88001"]


@pytest.mark.parametrize("invalid",[None,"","0",0,-1,"0150","150 ","1e2",True,150.0,[],{},10**18])
def test_present_invalid_departments_reject_in_discovery_and_metadata(invalid):
    result = client([page([row(invalid)])]).read_billing("60178","1")
    assert result["state"] == "unavailable" and not result["encounters"]
    calls = []
    with pytest.raises(AthenaClientError):
        client([page([row()]),row(invalid)],calls).read_billing("60178","1","88001")
    assert len(calls) == 2


@pytest.mark.parametrize("actual",["1","2","absent"])
def test_known_list_department_requires_matching_metadata_before_summary(actual):
    calls = []
    with pytest.raises(AthenaClientError):
        client([page([row()]),row(actual)],calls).read_billing("60178","1","88001")
    assert len(calls) == 2


@pytest.mark.parametrize("actual,expected,origin",[(150,"150","metadata"),("absent",None,"not_supplied")])
def test_absent_list_department_preserves_metadata_or_explicit_unknown(actual,expected,origin):
    listed,metadata = row("absent"),row(actual)
    result = client([page([listed]),metadata,SUMMARY,metadata]).read_billing("60178","1","88001")
    assert result["encounters"][0]["departmentid"] == {"state":"absent","value":None}
    assert result["note"]["sourceDepartmentId"] == expected and result["note"]["sourceDepartmentOrigin"] == origin
    assert result["departmentId"] == "1"


@pytest.mark.parametrize("key,value",[("patientid","60179"),("practiceid","195901"),("encounterid","88002")])
def test_patient_practice_and_selected_encounter_identity_remain_strict(key,value):
    calls = []
    with pytest.raises(AthenaClientError):
        client([page([row()]),{**row(),key:value}],calls).read_billing("60178","1","88001")
    assert len(calls) == 2


def test_cross_department_summary_requires_unchanged_after_metadata():
    calls = []
    with pytest.raises(AthenaClientError):
        client([page([row()]),row(),SUMMARY,row("1")],calls).read_billing("60178","1","88001")
    assert len(calls) == 4
