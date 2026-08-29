import assert from "node:assert/strict";
import test from "node:test";
import syntheticMcdWorklist from "./operationalMcdWorklist.golden.json" with { type: "json" };
import suggestedGolden from "./operationalSuggestedCodes.golden.json" with { type: "json" };
import {
  isExactEvidenceSpan, outcomeCompletesTask, recordPhoneAttempt,
  safeChaseTasks, visibleSuggestedCodes,
} from "./operationalDemoContract.js";

const clone = (value) => structuredClone(value);

test("golden backend suggestions satisfy the complete dormant display contract", () => {
  const { documents, payload } = suggestedGolden;
  const visible = visibleSuggestedCodes(payload, documents);
  assert.deepEqual(visible.map(({ code }) => code), payload.codes.map(({ code }) => code));
  for (const row of visible) {
    assert.equal(row.copy_text, row.code);
    assert.ok(row.evidence_spans.every((span) => isExactEvidenceSpan(span, documents)));
  }
});

test("suggestion posture, copy mismatch, and missing evidence fail closed", () => {
  const { documents } = suggestedGolden;
  const missingEnabled = clone(suggestedGolden.payload); delete missingEnabled.enabled;
  assert.deepEqual(visibleSuggestedCodes(missingEnabled, documents), []);
  for (const mutate of [
    (payload) => { payload.enabled = true; },
    (payload) => { payload.human_verification_required = false; },
    (payload) => { payload.pipeline.stages.reverse(); },
    (payload) => { payload.codes[0].copy_text = "99215"; },
    (payload) => { payload.codes[0].evidence_spans = []; },
  ]) {
    const payload = clone(suggestedGolden.payload); mutate(payload);
    const codes = visibleSuggestedCodes(payload, documents).map(({ code }) => code);
    if (payload.enabled === true || payload.human_verification_required === false
        || payload.pipeline.stages[0] !== "CodingAgent") assert.deepEqual(codes, []);
    else assert.ok(!codes.includes("99214"));
  }
});

test("evidence offsets use Python-compatible Unicode code points", () => {
  assert.equal(isExactEvidenceSpan({
    source: "signed_note", text: "documented visit", loc: { start: 4, end: 20 },
  }, { signed_note: "A 🩺 documented visit" }), true);
});

test("only the exact safe backend worklist shape admits phone tasks", () => {
  assert.equal(safeChaseTasks(syntheticMcdWorklist).length, 2);
  const mockAthena = clone(syntheticMcdWorklist); mockAthena.safety.athena_mode = "mock";
  assert.equal(safeChaseTasks(mockAthena).length, 2);
  for (const mutate of [
    (value) => { value.safety.scheduler_enabled = true; },
    (value) => { value.safety.athena_mode = "live"; },
    (value) => { value.counts.total = 1; },
    (value) => { value.tasks[0].person.voice_consent = false; },
    (value) => { value.tasks[0].direct_contract.known = false; },
    (value) => { value.tasks[0].person.phone = "+14155552671"; },
    (value) => { value.tasks[0].performed_by = "system"; },
    (value) => { value.tasks[0].automated = true; },
  ]) {
    const worklist = clone(syntheticMcdWorklist); mutate(worklist);
    assert.ok(safeChaseTasks(worklist).length < 2);
  }
});

test("phone records match backend fields and distinguish terminal outcomes", () => {
  const task = safeChaseTasks(syntheticMcdWorklist)[0];
  const record = recordPhoneAttempt(task, { outcome: "no_answer", minutes_spent: 3 }, {
    action_id: "local-1", actor_ref: "SYN-STAFF-DEMO-001",
    occurred_at: "2026-08-29T14:30:00Z",
  });
  assert.deepEqual(record, {
    action_id: "local-1", task_id: task.task_id, channel: "phone",
    performed_by: "staff", automated_outbound_initiated: false,
    manual_outreach_recorded: true, actor_ref: "SYN-STAFF-DEMO-001",
    outcome: "no_answer", minutes_spent: 3, occurred_at: "2026-08-29T14:30:00Z",
  });
  assert.equal(outcomeCompletesTask("no_answer"), false);
  assert.equal(outcomeCompletesTask("wrong_number"), false);
  assert.equal(outcomeCompletesTask("reached"), true);
  assert.equal(outcomeCompletesTask("declined"), true);
  assert.throws(() => recordPhoneAttempt(task, { outcome: "reached", minutes_spent: 481 }, {
    action_id: "local-2", actor_ref: "SYN-STAFF-DEMO-001",
    occurred_at: "2026-08-29T14:31:00Z",
  }), /invalid-minutes/);
});
