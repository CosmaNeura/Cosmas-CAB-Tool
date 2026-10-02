import test from "node:test";
import assert from "node:assert/strict";
import { canLaunchHeader, canPreviewHandoff, handoffAttempt, handoffSignIn, readHandoffFixtures, readHandoffReceipt, headerLaunchPath, headerLaunchRequest, headerLaunchSignIn,
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

test("launch never accepts incomplete provider-note or unknown operational source", async () => {
  for (const source of ["provider_completed_note", "unknown"]) {
    const detail = {id:"provider-local", dataClass:"operational", readOnly:false, encounterVersion:1,
      inferencePolicy:{allowed:true,reason:null}, headerSource:{ingestionSource:source}};
    await assert.rejects(readLaunchEncounter(detail,detail.id));
  }
});

test("provider launch requires explicit current policy and preserves exact note bytes", async () => {
  const note="Authored provider note",digest=Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(note))),n=>n.toString(16).padStart(2,"0")).join("");
  const detail={id:"provider-test",readOnly:false,dataClass:"operational",encounterVersion:1,
    signedNote:note,transcript:"",intake:"",encounterType:"office_established",placeOfService:"11",
    headerSource:{encounterId:"provider-test",encounterVersion:1,ingestionSource:"provider_completed_note",
      noteId:"note",noteVersion:"1",noteSha256:digest,bundleSha256:"a".repeat(64),supportingSourceDigests:[]}};
  await assert.rejects(readLaunchEncounter(detail,detail.id));
  for (const policy of [{allowed:true,reason:null},{allowed:false,reason:"operational_source_model_not_approved"}]) {
    const valid=await readLaunchEncounter({...detail,inferencePolicy:policy},detail.id);
    assert.deepEqual(valid.inferencePolicy,policy);assert.equal(valid.signedNote,note);
  }
  for (const policy of [null,{},true,{allowed:"true",reason:null},{allowed:false,reason:null},
    {allowed:true,reason:"operational_source_model_not_approved"},{allowed:true,reason:null,override:true}])
    await assert.rejects(readLaunchEncounter({...detail,inferencePolicy:policy},detail.id));
  await assert.rejects(readLaunchEncounter({...detail,signedNote:note+" altered",inferencePolicy:{allowed:true,reason:null}},detail.id));
});


test("handoff is an explicit route with no note, fixture, or return-path query inputs", () => {
  assert.deepEqual(headerLaunchRequest("?view=handoff"), { handoff: true });
  assert.equal(headerLaunchRequest("?view=provider"), null);
  for (const suffix of ["&view=handoff", "&view=header", "&encounterId=SYN-RC-0001", "&fixtureKey=SYN-RC-0001",
    "&note=text", "&operationId=anything", "&returnUrl=https://elsewhere", "&unknown="]) {
    assert.ok(headerLaunchRequest(`?view=handoff${suffix}`).error);
  }
  assert.equal(new URL(handoffSignIn(), "https://cab.example").searchParams.get("post_login_redirect_uri"), "/?view=handoff");
});

test("handoff needs provider access and both explicit server capabilities", () => {
  const session = { views: ["provider"], capabilities: { billingHeader: true, providerHandoff: true } };
  assert.equal(canPreviewHandoff(session), true);
  for (const value of [null, { ...session, views: ["chase"] }, { ...session, capabilities: {} },
    { ...session, capabilities: { billingHeader: true } }, { ...session, capabilities: { providerHandoff: true } },
    { ...session, capabilities: { billingHeader: true, providerHandoff: "true" } }]) assert.equal(canPreviewHandoff(value), false);
});

const fixture = { key: "SYN-RC-0001", label: "Synthetic visit 1", encounterId: "SYN-RC-0001" };
const descriptor = () => ({ fixtures: [{ ...fixture }], completionMeaning: "Completion is a synthetic fixture convention." });
const operationId = "7b5c4fd3-f69f-4466-967d-b3f0a1076ad3";
const attempt = () => handoffAttempt([fixture], fixture.key, null, () => operationId);
const response = () => ({ receipt: { operationId, receiptId: "handoff-receipt:opaque-1", encounterId: fixture.encounterId,
  encounterVersion: 2, headerSource: { encounterId: fixture.encounterId, encounterVersion: 2,
    ingestionSource: "synthetic_demo_release_candidate", noteId: "opaque:note", noteVersion: "2",
    noteSha256: "a".repeat(64), bundleSha256: "b".repeat(64), supportingSourceDigests: [] } },
  replayed: false, current: true, generation: "not_started", launchPath: headerLaunchPath(fixture.encounterId) });

test("fixture descriptors are bounded and contain unique server identities", () => {
  assert.deepEqual(readHandoffFixtures(descriptor()), descriptor());
  assert.deepEqual(readHandoffFixtures({ ...descriptor(), fixtures: [] }).fixtures, []);
  for (const edit of [d => { d.fixtures.push(fixture); }, d => { d.fixtures[0].key = "//elsewhere"; },
    d => { d.fixtures[0].encounterId = "../other"; }, d => { d.fixtures[0].label = "hidden\u0000text"; },
    d => { d.fixtures[0].label = "x".repeat(201); }, d => { d.completionMeaning = ""; },
    d => { d.fixtures = Array(101).fill(fixture); }]) {
    const value = descriptor(); edit(value); assert.throws(() => readHandoffFixtures(value));
  }
});

test("retry retains the original operation and cannot change its fixture", () => {
  let created = 0;
  const create = () => { created++; return operationId; };
  const original = handoffAttempt([fixture], fixture.key, null, create);
  assert.equal(handoffAttempt([fixture], fixture.key, original, create), original);
  assert.equal(handoffAttempt([fixture], fixture.key, original, create), original);
  assert.equal(created, 1);
  assert.equal(Object.isFrozen(original), true);
  assert.throws(() => handoffAttempt([fixture], "missing", original, create));
  assert.throws(() => handoffAttempt([{ ...fixture, encounterId: "SYN-RC-0002" }], fixture.key, original, create));
  assert.throws(() => handoffAttempt([fixture], fixture.key, null, () => "not-a-uuid"));
});

test("only a current matching receipt launches; a superseded replay never does", () => {
  assert.equal(readHandoffReceipt(response(), attempt()).launchPath, headerLaunchPath(fixture.encounterId));
  assert.equal(readHandoffReceipt({ ...response(), replayed: true }, attempt()).replayed, true);
  const old = { ...response(), replayed: true, current: false, launchPath: null };
  assert.deepEqual(readHandoffReceipt(old, attempt()), { receiptId: old.receipt.receiptId, replayed: true, current: false, launchPath: null });
  assert.throws(() => readHandoffReceipt({ ...old, launchPath: response().launchPath }, attempt()));
});

test("receipt permits pinned intake/transcript but rejects duplicate source identities", () => {
  const value = response(), pin = value.receipt.headerSource;
  pin.supportingSourceDigests = ["transcript", "intake"].map((kind) => ({ kind, id: `opaque:${kind}`, version: "2", sha256: "c".repeat(64) }));
  assert.equal(readHandoffReceipt(value, attempt()).current, true);
  for (const edit of [p => { p.supportingSourceDigests[1].id = p.noteId; },
    p => { p.supportingSourceDigests[1].kind = "transcript"; }, p => { p.supportingSourceDigests[1].version = "1"; }]) {
    const invalid = structuredClone(value); edit(invalid.receipt.headerSource);
    assert.throws(() => readHandoffReceipt(invalid, attempt()));
  }
});

for (const [label, edit] of [
  ["another operation", r => { r.receipt.operationId = "bd0f1d4b-d21a-459e-badc-be29e8b8c836"; }],
  ["another encounter", r => { r.receipt.encounterId = "SYN-RC-0002"; }],
  ["another source version", r => { r.receipt.headerSource.encounterVersion = 3; }],
  ["another source encounter", r => { r.receipt.headerSource.encounterId = "SYN-RC-0002"; }],
  ["operational source", r => { r.receipt.headerSource.ingestionSource = "operational"; }],
  ["invalid digest", r => { r.receipt.headerSource.noteSha256 = "unknown"; }],
  ["invalid version", r => { r.receipt.encounterVersion = 0; }],
  ["coerced current flag", r => { r.current = "true"; }],
  ["generation already started", r => { r.generation = "started"; }],
  ["absolute navigation", r => { r.launchPath = `https://cab.example${r.launchPath}`; }],
  ["foreign origin", r => { r.launchPath = "//elsewhere/?view=header"; }],
  ["extra query parameter", r => { r.launchPath += "&note=injected"; }],
  ["fragment navigation", r => { r.launchPath += "#elsewhere"; }],
  ["different launch target", r => { r.launchPath = headerLaunchPath("SYN-RC-0002"); }],
  ["unverified supporting source", r => { r.receipt.headerSource.supportingSourceDigests = [{ kind: "transcript" }]; }],
]) test(`receipt rejects ${label}`, () => {
  const value = response(); edit(value); assert.throws(() => readHandoffReceipt(value, attempt()));
});
