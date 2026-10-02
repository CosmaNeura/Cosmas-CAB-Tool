import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readAthenaView, readAthenaBilling, mergeAthenaBilling, athenaBillingReadBody, athenaPhoneReview, athenaSourceDepartment, athenaFailure, ambiguousAthenaFailure, fieldDisplay } from "./athenaCaseReview.js";
const fixture = JSON.parse(readFileSync(new URL("./athenaReview.fixture.json", import.meta.url)));
const source = () => structuredClone(fixture.source);
const applied = () => ({...source(), revision: 2, applied: structuredClone(fixture.applied)});

test("golden generated backend output validates across read, application and import", () => {
  assert.equal(readAthenaView({view:source()},fixture.chaseId).candidate.source.patientId,"60178");
  const view = readAthenaView({view:applied()},fixture.chaseId);
  assert.equal(view.applied.consent,"unknown");
  assert.match(readAthenaBilling(fixture.billing,view,"1","88001").note.plainText,/Bundled synthetic visit/);
  const imported = readAthenaView({view:{...view,revision:3,billingImport:fixture.import}},fixture.chaseId);
  assert.equal(imported.billingImport.source,"athena_staff_review_mock");
});

test("empty case stays empty, never creates implicit source or confirmation", () => {
  const empty = {schemaVersion:"athena-case-review.v1",chaseId:"new",revision:0,reviewId:null,candidate:null,applied:null};
  assert.equal(readAthenaView({view:empty},"new").candidate,null);
  assert.throws(() => readAthenaView({view:{...empty,applied:{}}},"new"));
});

for (const [name, change] of [
  ["wrong case", (v) => {v.chaseId="different";}],
  ["wrong practice", (v) => {v.candidate.source.vendorPracticeId="1";}],
  ["non-synthetic cohort", (v) => {v.candidate.source.patientId="1";}],
  ["invented consent", (v) => {v.candidate.contact.effectiveConsent.sms="granted";}],
  ["outreach authorized", (v) => {v.candidate.contact.outreachAuthorized=true;}],
  ["over-bound policy list", (v) => {v.candidate.insurance.policies=Array(16).fill(v.candidate.insurance.policies[0]);}],
  ["duplicate policy identity", (v) => {v.candidate.insurance.policies.push(v.candidate.insurance.policies[0]);}],
  ["invalid field shape", (v) => {v.candidate.patient.fields[0].value={nested:"untrusted"};}],
  ["missing source hash", (v) => {delete v.candidate.patient.fields[0].source.sha256;}],
  ["malformed timestamp", (v) => {v.candidate.source.observedAt="not-time";}],
]) test(`fails closed on ${name}`, () => {const v=source();change(v);assert.throws(() => readAthenaView({view:v},fixture.chaseId));});

test("note response is bound to case revision, selected patient, department and encounter", () => {
  for (const delta of [{caseRevision:1},{sourcePatientId:"60179"},{departmentId:"2"},{readyForHeader:true}]) {
    assert.throws(() => readAthenaBilling({...fixture.billing,...delta},applied(),"1","88001"));
  }
  assert.throws(() => readAthenaBilling(fixture.billing,applied(),"1","999"));
  assert.throws(() => readAthenaBilling({...fixture.billing,note:{...fixture.billing.note,plainText:"\ud800"}},applied(),"1","88001"));
});

test("pending writes classify malformed/timeout/server failure as ambiguous without replacing UUID", () => {
  for (const failure of [{},{status:429},{status:500},{status:503}]) assert.equal(ambiguousAthenaFailure(failure),true);
  for (const status of [403,404,409,422]) assert.equal(ambiguousAthenaFailure({status}),false);
});

test("failed reads name an available read action without claiming a timeout or uncertain write", () => {
  for (const failure of [{}, { status: 429 }, { status: 500 }, { status: 503 }]) {
    const billing = athenaFailure(failure, { kind: "billing" });
    const snapshot = athenaFailure(failure, { kind: "snapshot" });
    assert.match(billing, /Find encounters to read the source again/);
    assert.match(snapshot, /Refresh case review/);
    assert.doesNotMatch(billing + snapshot, /timed out|same request|uncertain/i);
    assert.match(athenaFailure(failure, { kind: "change" }), /Retry the same request/);
    assert.equal(ambiguousAthenaFailure(failure), true);
  }
});

test("only the explicit client deadline produces timeout guidance and mutations keep exact retry", () => {
  assert.match(athenaFailure({}, { kind: "billing", timedOut: true }), /timed out.*Find encounters/);
  assert.match(athenaFailure({}, { kind: "snapshot", timedOut: true }), /timed out.*Refresh case review/);
  assert.match(athenaFailure({}, { kind: "change", timedOut: true }), /uncertain; retry the same request/);
  assert.doesNotMatch(athenaFailure({ status: 409 }, { kind: "billing" }), /before applying/);
});

test("absent and invalid fields do not render nested/raw source content as known facts", () => {
  assert.equal(fieldDisplay(null),"Not supplied");
  assert.equal(fieldDisplay({state:"absent",value:"unsafe"}),"Not supplied");
  assert.equal(fieldDisplay({state:"invalid",value:"unsafe"}),"Unrecognized source value");
  assert.equal(fieldDisplay({state:"present",value:"DEMO-GROUP-7"}),"DEMO-GROUP-7");
});

test("national phone stays unnormalized until staff explicitly confirms the country", () => {
  for (const value of ["2025550123", "202-555-0123", "(202) 555-0123"]) {
    const contact = { kind: "mobile", formatStatus: "country_confirmation_required", field: { state: "present", value } };
    assert.deepEqual(athenaPhoneReview(contact), { selectable: true, needsCountry: true, normalizedValue: null });
    assert.equal(athenaPhoneReview(contact, "US").normalizedValue, "+12025550123");
    assert.equal(athenaPhoneReview(contact, "CA").normalizedValue, null);
    assert.equal(contact.field.value, value);
  }
});

test("existing international phones remain exact and unsupported source shapes stay disabled", () => {
  const contact = { kind: "home", formatStatus: "plausible", field: { state: "present", value: "+442079460123" } };
  assert.deepEqual(athenaPhoneReview(contact), { selectable: true, needsCountry: false, normalizedValue: "+442079460123" });
  assert.equal(athenaPhoneReview(contact, "US").normalizedValue, null);
  for (const value of ["2025550123 ext 8", "2025550123 / 2025550199", "12025550123", "0000000000", "2021550123", "2025550123\n"]) {
    assert.equal(athenaPhoneReview({ ...contact, formatStatus: "country_confirmation_required", field: { state: "present", value } }, "US").selectable, false);
  }
});

test("applied normalized phone requires exact original source and staff country provenance", () => {
  const view = applied();
  const phone = view.applied.contacts.find((c) => c.field === "mobile_phone");
  const raw = view.applied.patient.fields.find((f) => f.name === "mobile_phone");
  raw.value = "2025550123";
  phone.value = "+12025550123";
  phone.normalization = { sourceValue: raw.value, country: "US", confirmedByStaff: true };
  assert.equal(readAthenaView({ view }, fixture.chaseId).applied.contacts[0].consent, "unknown");
  for (const change of [
    (v) => { v.applied.contacts[0].normalization.country = "CA"; },
    (v) => { v.applied.contacts[0].normalization.confirmedByStaff = false; },
    (v) => { v.applied.contacts[0].value = "+12025550199"; },
    (v) => { v.applied.patient.fields.find((f) => f.name === "mobile_phone").value = "2025550199"; },
  ]) {
    const changed = structuredClone(view); change(changed);
    assert.throws(() => readAthenaView({ view: changed }, fixture.chaseId));
  }
});

function billingPage({ start = 0, count = 15, loadedCount = count, windowStart = 0,
  append = false, nextToken = `discovery-${start + count}`, selectionToken = nextToken,
  nextStartsWindow = loadedCount >= 145 } = {}) {
  return { ...structuredClone(fixture.billing), note: null,
    encounters: Array.from({ length: count }, (_, i) => ({ ...structuredClone(fixture.billing.encounters[0]),
      encounterid: { state: "present", value: String(88001 + start + i) } })),
    pageHashes: Array.from({ length: Math.max(1, Math.ceil(count / 5)) }, (_, i) => (start + i + 1).toString(16).padStart(64, "0")),
    state: nextToken ? "partial" : count ? "complete" : "empty",
    pagination: { loadedCount, windowStart, append, nextToken, selectionToken, nextStartsWindow } };
}
const findBilling = (page = billingPage(), view = applied()) => mergeAthenaBilling(null, page, view, "1");
const continueBilling = (previous, page, discoveryToken = previous.pagination.nextToken) =>
  mergeAthenaBilling(previous, page, applied(), "1", { continuation: true, discoveryToken });
function previewBilling(previous, encounter) {
  const index = previous.encounters.findIndex((row) => row.encounterid.value === encounter);
  return { ...billingPage({ count: 0 }), state: "complete",
    encounters: previous.encounters.slice(Math.floor(index / 5) * 5, Math.floor(index / 5) * 5 + 5),
    pagination: { ...previous.pagination, append: false },
    note: { ...structuredClone(fixture.billing.note), encounterId: encounter,
      metadata: { ...structuredClone(fixture.billing.note.metadata), encounterid: {state:"present",value:encounter} } } };
}

test("Find has no token; explicit continuation and selected preview forward their own opaque receipt", () => {
  const first = findBilling();
  first.pagination.nextToken = "next-page-receipt";
  first.pagination.selectionToken = "selected-page-receipt";
  assert.deepEqual(athenaBillingReadBody(fixture.chaseId, "1", first), {
    chaseId: fixture.chaseId, departmentId: "1", sourceEncounterId: null,
  });
  assert.equal(athenaBillingReadBody(fixture.chaseId, "1", first, { continuation: true }).discoveryToken, "next-page-receipt");
  assert.deepEqual(athenaBillingReadBody(fixture.chaseId, "1", first, { encounter: "88001" }), {
    chaseId: fixture.chaseId, departmentId: "1", sourceEncounterId: "88001", discoveryToken: "selected-page-receipt",
  });
  assert.throws(() => athenaBillingReadBody(fixture.chaseId, "1", first, { encounter: "99001" }));
  assert.throws(() => athenaBillingReadBody(fixture.chaseId, "1", null, { continuation: true }));
});

test("later pages append; selected-page preview preserves the full list, list state and source hashes", () => {
  const first = findBilling();
  const page = billingPage({ start: 15, count: 10, loadedCount: 25, append: true });
  const second = continueBilling(first, page);
  assert.equal(second.encounters.length, 25);
  assert.equal(second.pageHashes.length, 5);
  const selected = "88025";
  const preview = mergeAthenaBilling(second, previewBilling(second, selected), applied(), "1",
    { encounter: selected, discoveryToken: second.pagination.selectionToken });
  assert.deepEqual(preview.encounters, second.encounters);
  assert.deepEqual(preview.pageHashes, second.pageHashes);
  assert.equal(preview.state, "partial");
  assert.equal(preview.note.encounterId, selected);
  assert.equal(preview.pagination.selectionToken, page.pagination.selectionToken);
  const third = continueBilling(preview, billingPage({ start: 25, count: 10, loadedCount: 35, append: true }));
  assert.deepEqual(third.note, preview.note);
  assert.equal(third.pagination.selectionToken, "discovery-35");
});

test("the exact last continuation replay is idempotent, including after note preview", () => {
  const first = findBilling(), page = billingPage({ start: 15, count: 10, loadedCount: 25, append: true });
  const second = continueBilling(first, page);
  const preview = mergeAthenaBilling(second, previewBilling(second, "88025"), applied(), "1",
    { encounter: "88025", discoveryToken: second.pagination.selectionToken });
  const replay = Object.fromEntries(Object.entries(structuredClone(page)).reverse());
  assert.equal(continueBilling(preview, replay, first.pagination.nextToken), preview);
  assert.equal(preview.encounters.length, 25);
  assert.equal(preview.pageHashes.length, 5);
  const third = continueBilling(preview, billingPage({ start: 25, count: 10, loadedCount: 35, append: true }));
  assert.throws(() => continueBilling(third, page, first.pagination.nextToken), { status: 409 });
  assert.equal(third.encounters.length, 35);
});

test("rolling windows permit explicit traversal beyond 150 without retaining older rows or notes", () => {
  let current = findBilling();
  for (let window = 0; window < 3; window++) {
    while (!current.pagination.nextStartsWindow) {
      const loadedCount = current.encounters.length + 10;
      current = continueBilling(current, billingPage({ start: current.pagination.windowStart + current.encounters.length,
        count: 10, loadedCount, windowStart: current.pagination.windowStart, append: true }));
    }
    assert.ok(current.encounters.length <= 150);
    assert.ok(current.pageHashes.length <= 30);
    const selected = current.encounters.at(-1).encounterid.value;
    current = mergeAthenaBilling(current, previewBilling(current, selected), applied(), "1",
      { encounter: selected, discoveryToken: current.pagination.selectionToken });
    const start = current.pagination.windowStart + current.encounters.length;
    const requestToken = current.pagination.nextToken;
    const older = billingPage({ start, count: 10, windowStart: start });
    current = continueBilling(current, older);
    assert.equal(current.encounters.length, 10);
    assert.equal(current.note, null);
    assert.equal(current.pagination.windowStart, start);
    assert.equal(continueBilling(current, older, requestToken), current);
  }
  assert.ok(current.pagination.windowStart > 300);
});

test("end-of-source continuation retains the visible window and has no next request", () => {
  const current = continueBilling(findBilling(), billingPage({ start: 15, count: 3, loadedCount: 18,
    append: true, nextToken: null, selectionToken: "last-selection" }));
  assert.equal(current.encounters.length, 18);
  assert.equal(current.state, "complete");
  assert.equal(current.pagination.nextToken, null);
  assert.throws(() => athenaBillingReadBody(fixture.chaseId, "1", current, { continuation: true }));
});

test("malformed or conflicting continuation leaves previous rows, selected note, and receipts unchanged", () => {
  let previous = findBilling();
  previous = mergeAthenaBilling(previous, previewBilling(previous, "88001"), applied(), "1",
    { encounter: "88001", discoveryToken: previous.pagination.selectionToken });
  const frozen = structuredClone(previous);
  for (const change of [
    (v) => { v.encounters[0] = structuredClone(previous.encounters[0]); },
    (v) => { v.encounters[0] = { ...structuredClone(previous.encounters[0]), status: { state: "present", value: "OPEN" } }; },
    (v) => { v.encounters[1] = structuredClone(v.encounters[0]); },
    (v) => { v.encounters[0].status = null; },
    (v) => { v.pagination.loadedCount = 24; },
    (v) => { v.pagination.windowStart = 5; },
    (v) => { v.pagination.append = false; },
    (v) => { delete v.pagination; },
    (v) => { v.departmentId = "2"; },
    (v) => { v.sourcePatientId = "60179"; },
    (v) => { v.caseRevision = 3; },
  ]) {
    const response = billingPage({ start: 15, count: 10, loadedCount: 25, append: true });
    change(response);
    assert.throws(() => continueBilling(previous, response));
    assert.deepEqual(previous, frozen);
  }
});

test("local case, review, patient, and department changes reject merges using prior discovery", () => {
  const previous = findBilling(), response = billingPage({ start: 15, count: 10, loadedCount: 25, append: true });
  for (const change of [
    (v) => { v.chaseId = "other-case"; },
    (v) => { v.caseBinding = "b".repeat(64); },
    (v) => { v.reviewId = "00000000-0000-0000-0000-000000000000"; },
    (v) => { v.revision++; },
    (v) => { v.candidate.source.patientId = "60179"; },
    (v) => { v.candidate.sourceDigest = "b".repeat(64); },
  ]) {
    const view = applied(); change(view);
    assert.throws(() => mergeAthenaBilling(previous, response, view, "1",
      { continuation: true, discoveryToken: previous.pagination.nextToken }), { status: 409 });
  }
  assert.throws(() => mergeAthenaBilling(previous, response, applied(), "2",
    { continuation: true, discoveryToken: previous.pagination.nextToken }), { status: 409 });
  const fresh = findBilling();
  assert.equal(fresh.encounters.length, 15);
  assert.equal(fresh.lastDiscoveryPage, undefined);
});

test("preview rejects wrong receipt, changed rows, mismatched window, and oversized selected page", () => {
  const previous = findBilling();
  for (const change of [
    (v) => { v.pagination.loadedCount++; },
    (v) => { v.pagination.windowStart = 5; },
    (v) => { v.pagination.nextToken = "other-token"; },
    (v) => { v.pagination.selectionToken = "other-token"; },
    (v) => { v.pagination.nextStartsWindow = true; },
    (v) => { v.pagination.append = true; },
    (v) => { v.encounters[0].status.value = "CHANGED"; },
    (v) => { v.encounters.push(structuredClone(previous.encounters[5])); },
  ]) {
    const response = structuredClone(previewBilling(previous, "88001")); change(response);
    assert.throws(() => mergeAthenaBilling(previous, response, applied(), "1",
      { encounter: "88001", discoveryToken: previous.pagination.selectionToken }), { status: 409 });
  }
  assert.throws(() => mergeAthenaBilling(previous, previewBilling(previous, "88001"), applied(), "1",
    { encounter: "88001", discoveryToken: "wrong-request-token" }), { status: 409 });
});

test("pagination wire fields are bounded and malformed values fail closed", () => {
  for (const delta of [
    { loadedCount: -1 }, { loadedCount: 14 }, { loadedCount: 151 }, { loadedCount: 15.5 },
    { windowStart: -1 }, { windowStart: 0.5 }, { windowStart: Number.MAX_SAFE_INTEGER + 1 },
    { append: "false" }, { nextStartsWindow: 1 }, { nextToken: "" },
    { nextToken: "x".repeat(12289) }, { selectionToken: null }, { selectionToken: "bad\u0000token" },
  ]) {
    const page = billingPage(); Object.assign(page.pagination, delta);
    assert.throws(() => findBilling(page));
  }
  assert.throws(() => findBilling({ ...billingPage(), pagination: null }));
  assert.throws(() => findBilling(billingPage({ count: 16 })));
  assert.throws(() => findBilling(billingPage({ windowStart: 5 })));
  assert.throws(() => findBilling(billingPage({ append: true })));
});

test("legacy and signing-unavailable first-window previews remain compatible without a token", () => {
  for (const legacy of [false, true]) {
    const page = billingPage({ nextToken: null, selectionToken: null });
    if (legacy) delete page.pagination;
    const previous = findBilling(page);
    const response = { ...structuredClone(page), note: structuredClone(fixture.billing.note) };
    const preview = mergeAthenaBilling(previous, response, applied(), "1", { encounter: "88001" });
    assert.equal(preview.encounters.length, 15);
    assert.equal(preview.note.encounterId, "88001");
    assert.equal(athenaBillingReadBody(fixture.chaseId, "1", preview, { encounter: "88001" }).discoveryToken, undefined);
  }
});

test("failed continuation guidance preserves read-only behavior and stale discovery requires Find", () => {
  for (const error of [{}, { status: 500 }, { status: 429 }]) {
    const message = athenaFailure(error, { kind: "billing", continuation: true });
    assert.match(message, /loaded encounters and selected note are unchanged/);
    assert.match(message, /Load more encounters/);
    assert.doesNotMatch(message, /uncertain|retry the same request/i);
  }
  assert.match(athenaFailure({ status: 409 }, { kind: "billing", continuation: true }), /Find encounters.*before loading, previewing, or importing/);
  assert.match(athenaFailure({ status: 409 }, { kind: "billing", continuation: true }), /case review changed.*Refresh case review first/);
  assert.match(athenaFailure({}, { kind: "billing", continuation: true, timedOut: true }), /timed out.*unchanged/);
});

function crossDepartmentBilling({ listed = "150", metadata = "150" } = {}) {
  const value = structuredClone(fixture.billing);
  const field = (id) => ({state:id === null ? "absent" : "present",value:id});
  value.departmentIdMeaning = "chart_query";
  value.encounters[0].departmentid = field(listed);
  value.note.metadata.departmentid = field(metadata);
  value.note.sourceDepartmentId = metadata;
  value.note.sourceDepartmentOrigin = listed !== null ? "list_and_metadata" : metadata !== null ? "metadata" : "not_supplied";
  return value;
}

test("query department stays bound while actual encounter department can differ", () => {
  const value = crossDepartmentBilling();
  const result = readAthenaBilling(value,applied(),"1","88001");
  assert.equal(result.departmentId,"1");
  assert.equal(result.note.sourceDepartmentId,"150");
  assert.equal(athenaSourceDepartment(result.note.sourceDepartmentId),"150");
  const list = findBilling({...value,note:null});
  assert.equal(athenaBillingReadBody(fixture.chaseId,"1",list,{encounter:"88001"}).departmentId,"1");
  assert.throws(() => readAthenaBilling(value,applied(),"150","88001"));
});

test("known list department requires matching metadata, never request fallback", () => {
  for (const metadata of ["1",null]) {
    assert.throws(() => readAthenaBilling(crossDepartmentBilling({metadata}),applied(),"1","88001"));
  }
  for (const change of [
    v => {v.note.sourceDepartmentId = "1";},
    v => {v.note.sourceDepartmentOrigin = "metadata";},
    v => {delete v.note.sourceDepartmentOrigin;},
    v => {v.note.metadata.encounterid.value = "88002";},
    v => {v.departmentIdMeaning = "encounter";},
  ]) {
    const value = crossDepartmentBilling();change(value);
    assert.throws(() => readAthenaBilling(value,applied(),"1","88001"));
  }
});

test("absent source observations remain explicit and metadata-only department is labelled", () => {
  for (const metadata of ["150",null]) {
    const result = readAthenaBilling(crossDepartmentBilling({listed:null,metadata}),applied(),"1","88001");
    assert.equal(result.note.sourceDepartmentOrigin,metadata === null ? "not_supplied" : "metadata");
    assert.equal(athenaSourceDepartment(result.note.sourceDepartmentId),metadata ?? "Not supplied");
  }
});

test("present null and malformed departments cannot become visible known values", () => {
  for (const bad of [{state:"null",value:null},{state:"present",value:null},
    {state:"present",value:"0150"},{state:"present",value:150},{state:"absent",value:"150"}]) {
    const value = crossDepartmentBilling();value.encounters[0].departmentid = bad;
    assert.throws(() => readAthenaBilling(value,applied(),"1","88001"));
  }
});

test("new import provenance is additive and old receipts never infer source department", () => {
  const legacy = readAthenaView({view:{...applied(),revision:3,billingImport:fixture.import}},fixture.chaseId);
  assert.equal(athenaSourceDepartment(legacy.billingImport.sourceDepartmentId),"Not supplied");
  const imported = {...structuredClone(fixture.import),departmentIdMeaning:"chart_query",
    sourceDepartmentId:"150",sourceDepartmentOrigin:"list_and_metadata"};
  assert.equal(readAthenaView({view:{...applied(),revision:3,billingImport:imported}},fixture.chaseId).billingImport.sourceDepartmentId,"150");
  for (const delta of [{sourceDepartmentId:"0150"},{sourceDepartmentId:null},{sourceDepartmentOrigin:"query"},
    {departmentIdMeaning:"encounter"},{departmentId:"0"}]) {
    assert.throws(() => readAthenaView({view:{...applied(),revision:3,billingImport:{...imported,...delta}}},fixture.chaseId));
  }
});
