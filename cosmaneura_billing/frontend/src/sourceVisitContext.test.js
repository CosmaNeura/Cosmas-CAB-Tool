import test from "node:test";
import assert from "node:assert/strict";
import { boundSourceVisitContext, readSourceVisitContext, sourceVisitContextDisplay, sourceVisitContextText, validServiceDate } from "./sourceVisitContext.js";

const pin = {encounterId:"context-test", encounterVersion:1, ingestionSource:"provider_completed_note",
  noteId:"note:1", noteVersion:"1", noteSha256:"a".repeat(64), bundleSha256:"b".repeat(64), supportingSourceDigests:[]};
const provider = () => ({schemaVersion:"cab.source-visit-context.v1", expectedSource:structuredClone(pin),
  origin:"provider_manifest",sourceSha256:"c".repeat(64),serviceDate:"2024-02-29",noteStatus:"completed",statusMeaning:"provider_attested"});

test("service dates remain civil dates, including leap days, without timestamp inference", () => {
  for (const date of ["0001-01-01","2024-02-29","2000-02-29","9999-12-31"]) assert.equal(validServiceDate(date),true);
  for (const date of [null,"","0000-01-01","2023-02-29","1900-02-29","2026-02-31","2026-04-31","2026-13-01",
    "09/30/2026","2026-09-30T23:00:00-07:00"," 2026-09-30",20260930]) assert.equal(validServiceDate(date),false);
});

test("provider completion and signed claims retain their limited attestation meaning", () => {
  const value=provider();
  assert.deepEqual(readSourceVisitContext(value,pin),value);
  assert.equal(sourceVisitContextDisplay(value,pin).serviceDate,"2024-02-29");
  assert.match(sourceVisitContextText(value,pin),/Completed \(provider-confirmed\)/);
  assert.match(sourceVisitContextText(value,pin),/upstream signature not independently verified/);
  value.noteStatus="signed";value.serviceDate=null;
  assert.match(sourceVisitContextText(value,pin),/Signed \(provider-confirmed\)/);
  assert.match(sourceVisitContextText(value,pin),/Date of service: Unavailable from source/);
});

test("Athena raw status is not translated into a signature or completion claim", () => {
  const source={...pin,ingestionSource:"athena_staff_review_operational"};
  const value={...provider(),expectedSource:source,origin:"athena_import",noteStatus:"CLOSED",statusMeaning:"source_reported"};
  const display=sourceVisitContextDisplay(value,source);
  assert.equal(display.noteStatus,"CLOSED");assert.match(display.qualification,/Source-reported/);
  value.noteStatus=null;assert.equal(sourceVisitContextDisplay(value,source).noteStatus,"Unavailable from source");
  value.serviceDate=null;assert.deepEqual(readSourceVisitContext(value,source),value);
});

test("old missing metadata and explicit unavailable context never borrow a service date or status", () => {
  assert.equal(readSourceVisitContext(undefined,pin),null);
  const unavailable={...provider(),origin:"unavailable",sourceSha256:null,serviceDate:null,noteStatus:null,statusMeaning:"unavailable"};
  assert.deepEqual(readSourceVisitContext(unavailable,pin),unavailable);
  assert.equal(sourceVisitContextText(unavailable,pin),"Date of service: Unavailable from source\nNote status: Unavailable from source");
});

test("malformed, foreign and stale context is rejected and suppressed independently of stored header", () => {
  for (const mutate of [v=>{v.schemaVersion="future";},v=>{v.extra=true;},v=>{v.sourceSha256="bad";},v=>{v.serviceDate="2026-02-31";},
    v=>{v.expectedSource.encounterId="other";},v=>{v.expectedSource.encounterVersion=2;},v=>{v.expectedSource.noteSha256="d".repeat(64);},
    v=>{v.statusMeaning="source_reported";},v=>{v.noteStatus="draft";},v=>{v.origin="unavailable";},
    v=>{v.origin="athena_import";v.statusMeaning="source_reported";},v=>{v.completedAt="2026-09-30T12:00:00Z";}]) {
    const value=provider();mutate(value);
    assert.throws(()=>readSourceVisitContext(value,pin));assert.equal(boundSourceVisitContext(value,pin),null);
    assert.equal(sourceVisitContextDisplay(value,pin).serviceDate,"Unavailable from source");
  }
  const source={...pin,ingestionSource:"athena_staff_review_mock"};
  for (const status of ["x".repeat(129),"CLOSED\nSIGNED","\u0000","\ud800",false]) {
    const value={...provider(),expectedSource:source,origin:"athena_import",statusMeaning:"source_reported",noteStatus:status};
    assert.throws(()=>readSourceVisitContext(value,source));
  }
});
