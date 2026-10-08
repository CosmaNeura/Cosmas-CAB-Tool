import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {applyProgressRefresh, headerProgressFromView, headerProgressLabel, readHeaderProgress} from "./headerProgress.js";
const {reviewed} = JSON.parse(readFileSync(new URL("./headerProgress.golden.json", import.meta.url)));

test("golden persisted revision zero is distinct from session labels", () => {
  assert.deepEqual(readHeaderProgress(reviewed), reviewed);
  assert.equal(headerProgressLabel(reviewed), "Revision 0 · coder review recorded");
  assert.equal(headerProgressLabel({...reviewed, status: "review_required", revisionNumber: 1}), "Revision 1 · fresh coder review required");
  assert.equal(headerProgressLabel(undefined), "Saved progress unavailable");
});

test("malformed, unbound or inconsistent progress never claims review", () => {
  for (const value of [null, [], Object.assign(Object.create({hidden: true}), reviewed),
    {...reviewed, extra: true}, {...reviewed, encounterVersion: true}, {...reviewed, bundleSha256: "bad"},
    {...reviewed, reviewedRevision: true}, {...reviewed, reviewedRevision: 1}, {...reviewed, status: "review_required"},
    {...reviewed, status: "awaiting_review"}, {...reviewed, status: "not_generated"}, {...reviewed, ingestionSource: ""}]) {
    assert.equal(readHeaderProgress(value), null);
    assert.equal(headerProgressLabel(value), "Saved progress unavailable");
  }
});

test("newer editor confirmation or pending invalidation defeats late list response", () => {
  const current = {A: null};
  assert.strictEqual(applyProgressRefresh(current, [{id: "A", headerProgress: reviewed}], 1, 2), current);
  assert.deepEqual(applyProgressRefresh(current, [{id: "A", headerProgress: reviewed}], 2, 2), {A: reviewed});
  const pin = reviewed;
  const view = {header: {expectedSource: pin}, revisionNumber: 0, signoff: {headerRevision: 0}};
  assert.deepEqual(headerProgressFromView(view, pin), reviewed);
  assert.equal(headerProgressFromView(view, {...pin, ingestionSource: "changed_provenance"}), null);
  assert.equal(headerProgressFromView(view, {...pin, bundleSha256: "b".repeat(64)}), null);
});
