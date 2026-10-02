import test from "node:test";
import assert from "node:assert/strict";
import { canLaunchHeader, headerLaunchPath, headerLaunchRequest, headerLaunchSignIn,
  readLaunchEncounter, isOperationalHeaderEntry } from "./providerHeaderLaunch.js";

test("launch links select exactly one ID and preserve only the same-origin route through login", () => {
  assert.equal(headerLaunchRequest(""), null);
  assert.deepEqual(headerLaunchRequest("?view=header&encounterId=ENC-100237"), { encounterId: "ENC-100237" });
  for (const query of ["?view=header", "?view=header&view=provider&encounterId=one", "?view=header&encounterId=one&encounterId=two",
    "?view=header&encounterId=one&note=injected", "?view=header&encounterId=https://elsewhere", "?view=header&encounterId=%00"]) {
    assert.ok(headerLaunchRequest(query).error);
  }
  const login = new URL(headerLaunchSignIn("ENC:100237"), "https://cab.example");
  assert.equal(login.pathname, "/.auth/login/aad");
  assert.equal(login.searchParams.get("post_login_redirect_uri"), "/?view=header&encounterId=ENC%3A100237");
  assert.throws(() => headerLaunchSignIn("//elsewhere"));
  assert.equal(canLaunchHeader({ views: ["provider"], capabilities: { billingHeader: true } }), true);
  assert.equal(canLaunchHeader({ views: ["chase"], capabilities: { billingHeader: true } }), false);
  assert.equal(canLaunchHeader({ views: ["provider"] }), false);
});

test("launch detail pins the selected server-owned synthetic note; mismatches never mount the header", async () => {
  const note = "Synthetic signed note";
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(note))), (n) => n.toString(16).padStart(2, "0")).join("");
  const detail = { id: "ENC-100237", readOnly: true, dataClass: "synthetic_demo_release_candidate", encounterVersion: 1,
    signedNote: note, transcript: "", intake: "", encounterType: "office", placeOfService: "10",
    headerSource: { encounterId: "ENC-100237", encounterVersion: 1, ingestionSource: "synthetic_demo_release_candidate",
      noteId: "local:note", noteVersion: "1", noteSha256: digest, bundleSha256: "a".repeat(64), supportingSourceDigests: [] } };
  assert.equal((await readLaunchEncounter(detail, detail.id)).signedNote, note);
  for (const change of [d => { d.id = "other"; }, d => { d.readOnly = false; }, d => { d.signedNote += " changed"; },
    d => { d.headerSource.encounterVersion = 2; }, d => { d.headerSource.ingestionSource = "operational"; }, d => { d.transcript = "Unpinned text"; }]) {
    const modified = structuredClone(detail); change(modified);
    await assert.rejects(readLaunchEncounter(modified, detail.id));
  }
});

test("operational Athena launch preserves either explicit model-policy outcome and exact source evidence", async () => {
  const note = "Authored synthetic note for operational admission tests";
  const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(note))), (n) => n.toString(16).padStart(2, "0")).join("");
  const detail = { id: "athena-review-local", readOnly: false, dataClass: "operational", encounterVersion: 1,
    signedNote: note, transcript: "", intake: "", encounterType: "office_established", placeOfService: "11",
    inferencePolicy: { allowed: false, reason: "operational_source_model_not_approved" },
    headerSource: { encounterId: "athena-review-local", encounterVersion: 1, ingestionSource: "athena_staff_review_operational",
      noteId: "source:note", noteVersion: "1", noteSha256: digest, bundleSha256: "a".repeat(64), supportingSourceDigests: [] } };
  const result = await readLaunchEncounter(detail, detail.id);
  assert.equal(result.signedNote, note);
  assert.equal(result.inferencePolicy.allowed, false);
  const context={schemaVersion:"cab.source-visit-context.v1",expectedSource:structuredClone(detail.headerSource),
    origin:"athena_import",sourceSha256:"c".repeat(64),serviceDate:"2026-09-29",noteStatus:"CLOSED",statusMeaning:"source_reported"};
  const contextual=await readLaunchEncounter({...detail,sourceVisitContext:context},detail.id);
  assert.deepEqual(contextual.sourceVisitContext,context);
  context.expectedSource.encounterVersion=2;
  const staleContext=await readLaunchEncounter({...detail,sourceVisitContext:context},detail.id);
  assert.equal(staleContext.sourceVisitContext,null);assert.equal(staleContext.signedNote,note);
  const approved=structuredClone(detail);approved.inferencePolicy={allowed:true,reason:null};
  assert.deepEqual((await readLaunchEncounter(approved,approved.id)).inferencePolicy,{allowed:true,reason:null});
  assert.equal(isOperationalHeaderEntry(detail),true);assert.equal(isOperationalHeaderEntry(approved),true);
  for (const policy of [null,{}, {allowed:true}, {allowed:false,reason:null}, {allowed:'true',reason:null},
    {allowed:true,reason:'operational_source_model_not_approved'}, {allowed:true,reason:null,override:true}]) {
    const invalid={...approved,inferencePolicy:policy};assert.equal(isOperationalHeaderEntry(invalid),false);
    await assert.rejects(readLaunchEncounter(invalid,detail.id));
  }
  for (const change of [d=>{d.signedNote+=' changed';},d=>{d.headerSource.noteSha256='f'.repeat(64);},
    d=>{d.headerSource.ingestionSource='unknown';},d=>{d.headerSource.encounterId='other';}]) {
    const invalid=structuredClone(approved);change(invalid);await assert.rejects(readLaunchEncounter(invalid,detail.id));
  }
  for (const entry of [{...approved,id:'//elsewhere'},{...approved,readOnly:true},{...approved,dataClass:'synthetic'},
    {id:approved.id,dataClass:'operational',readOnly:false,sourceAdmission:{grantSha256:'a'.repeat(64)}}])
    assert.equal(isOperationalHeaderEntry(entry),false);
  for (const change of [d => { delete d.inferencePolicy; }, d => { d.inferencePolicy.allowed = true; },
    d => { d.inferencePolicy.reason = "read-approved"; }, d => { d.inferencePolicy.override = true; },
    d => { d.headerSource.ingestionSource = "future_unknown_source"; }, d => { d.signedNote += " changed"; }]) {
    const modified = structuredClone(detail); change(modified);
    await assert.rejects(readLaunchEncounter(modified, detail.id));
  }
});

test("launch never accepts an unimplemented provider-note or unknown operational source", async () => {
  for (const source of ["provider_completed_note", "unknown"]) {
    const detail = {id:"provider-local", dataClass:"operational", readOnly:false, encounterVersion:1,
      inferencePolicy:{allowed:true,reason:null}, headerSource:{ingestionSource:source}};
    await assert.rejects(readLaunchEncounter(detail,detail.id));
  }
});
