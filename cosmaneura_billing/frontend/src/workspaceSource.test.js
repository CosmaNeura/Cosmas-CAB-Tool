import test from "node:test";
import assert from "node:assert/strict";
import { isWorkspaceEncounterEntry, readWorkspaceEncounter, workspaceInferenceAllowed,
  readWorkspaceResult, loadCoderWorkspace } from "./workspaceSource.js";

const approved = { allowed: true, reason: null };
const denied = { allowed: false, reason: "operational_source_model_not_approved" };
async function detail(source = "provider_completed_note", policy = approved) {
  const text = "Authored local completed note\r\nOriginal text remains unchanged.";
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))), n => n.toString(16).padStart(2, "0")).join("");
  return { id: "registered-encounter", encounterVersion: 2, dataClass: "operational", readOnly: false,
    ingestionSource: source, inferencePolicy: policy, encounterType: "office_established", placeOfService: "11", signedNote: text, intake: "", transcript: "", submittedEm: "99213",
    headerSource: { encounterId: "registered-encounter", encounterVersion: 2, ingestionSource: source,
      noteId: "completed-note", noteVersion: "2", noteSha256: hash, bundleSha256: "a".repeat(64), supportingSourceDigests: [] } };
}

for (const source of ["provider_completed_note", "athena_staff_review_operational"]) {
  test(`${source} admits a source-verified workspace with either current policy outcome`, async () => {
    for (const policy of [approved, denied]) {
      const value = await detail(source, policy);
      assert.equal(isWorkspaceEncounterEntry(value), true);
      assert.equal(isWorkspaceEncounterEntry(value, false), false);
      const read = await readWorkspaceEncounter(value, value.id, value);
      assert.equal(read.signedNote, value.signedNote);
      assert.equal(read.submittedEm, "99213");
      assert.equal(workspaceInferenceAllowed(read), policy.allowed);
    }
  });
}

test("unknown sources and malformed or absent source policies never enter the queue", async () => {
  const value = await detail();
  for (const change of [d => delete d.ingestionSource, d => {d.ingestionSource = "unregistered_import";},
    d => delete d.inferencePolicy, d => {d.inferencePolicy = {allowed: true};},
    d => {d.inferencePolicy = {...approved, override: true};}, d => {d.readOnly = true;}]) {
    const bad = structuredClone(value); change(bad);
    assert.equal(isWorkspaceEncounterEntry(bad), false);
    await assert.rejects(readWorkspaceEncounter(bad, bad.id, bad));
  }
});

test("selected source and exact clinical documents must agree before rendering or inference", async () => {
  const value = await detail();
  for (const change of [d => {d.id = "other";}, d => {d.encounterVersion = 3;},
    d => {d.ingestionSource = "athena_staff_review_operational";},
    d => {d.headerSource.ingestionSource = "athena_staff_review_operational";},
    d => {d.signedNote += " appended";}, d => {d.transcript = "Unpinned document";},
    d => {d.dataClass = "synthetic_demo_release_candidate"; d.readOnly = true;}]) {
    const bad = structuredClone(value); change(bad);
    await assert.rejects(readWorkspaceEncounter(bad, value.id, value));
  }
});

test("synthetic source remains compatible with header-on and older header-off servers", async () => {
  const value = await detail("synthetic_demo_release_candidate");
  value.dataClass = value.ingestionSource; value.readOnly = true; delete value.ingestionSource; delete value.inferencePolicy;
  const verified = await readWorkspaceEncounter(value, value.id, value);
  assert.equal(workspaceInferenceAllowed(verified), true);
  delete value.headerSource;
  const legacy = await readWorkspaceEncounter(value, value.id, value);
  assert.equal(legacy.headerSource, null);
  assert.equal(legacy.signedNote, value.signedNote);
});

test("coder model request follows verified detail and is never made under denied policy", async () => {
  for (const policy of [approved, denied]) {
    const value = await detail("provider_completed_note", policy), paths = [];
    const result = {...value, encounterId: value.id};
    const loaded = await loadCoderWorkspace(value.id, value, async (path, body) => {
      paths.push(path); assert.deepEqual(body, {encounterId: value.id});
      return path.endsWith("/detail") ? value : result;
    });
    assert.deepEqual(paths, policy.allowed ? ["/api/encounters/detail", "/api/encounters/coder-view"] : ["/api/encounters/detail"]);
    assert.equal(loaded.encounter.signedNote, value.signedNote);
    assert.equal(Boolean(loaded.review), policy.allowed);
  }
});

test("failed source verification starts no model request", async () => {
  const value = await detail(), paths = [];
  await assert.rejects(loadCoderWorkspace(value.id, value, async (path) => {
    paths.push(path); return {...value, signedNote: "changed"};
  }));
  assert.deepEqual(paths, ["/api/encounters/detail"]);
});

test("result cannot change source, version, data class, or permission after loading", async () => {
  const value = await detail(), encounter = await readWorkspaceEncounter(value, value.id, value);
  for (const idField of ["encounterId", "visit_id"]) {
    const result = {...value, [idField]: value.id};
    assert.equal(readWorkspaceResult(result, encounter, idField), result);
    for (const change of [d => {d[idField] = "other";}, d => {d.encounterVersion++;},
      d => {d.ingestionSource = "athena_staff_review_operational";}, d => delete d.ingestionSource,
      d => {d.inferencePolicy = denied;}, d => {d.readOnly = true;},
      d => {d.dataClass = "synthetic_demo_release_candidate";}]) {
      const bad = structuredClone(result); change(bad);
      assert.throws(() => readWorkspaceResult(bad, encounter, idField));
    }
  }
});
