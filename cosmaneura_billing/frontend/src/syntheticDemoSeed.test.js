import assert from "node:assert/strict";
import test from "node:test";

import {
  seedConfirmationIsSafe,
  syntheticSeedConfirmationMessage,
  syntheticSeedErrorMessage,
} from "./syntheticDemoSeed.js";


const DIGEST = "a".repeat(64);
const DESCRIPTOR = Object.freeze({ seedDigest: DIGEST, caseCount: 4 });
const CONFIRMATION = Object.freeze({
  seedDigest: DIGEST,
  installedCaseCount: 4,
  openTaskCount: 4,
  replayed: false,
  outbound: Object.freeze({
    schedulerEnabled: false,
    patientMessagingEnabled: false,
    operationallyActivated: false,
    automaticContact: false,
    transport: "record_only",
  }),
});


test("accepts the exact installed count and a bounded open-task count", () => {
  assert.equal(seedConfirmationIsSafe(CONFIRMATION, DESCRIPTOR), true);
  assert.equal(seedConfirmationIsSafe({
    ...CONFIRMATION, openTaskCount: 3, replayed: true,
  }, DESCRIPTOR), true);
  assert.equal(seedConfirmationIsSafe({ ...CONFIRMATION, seedDigest: "b".repeat(64) }, DESCRIPTOR), false);
  assert.equal(seedConfirmationIsSafe({ ...CONFIRMATION, installedCaseCount: 3 }, DESCRIPTOR), false);
  assert.equal(seedConfirmationIsSafe({ ...CONFIRMATION, openTaskCount: 5 }, DESCRIPTOR), false);
  assert.equal(seedConfirmationIsSafe({ ...CONFIRMATION, openTaskCount: -1 }, DESCRIPTOR), false);
  assert.equal(seedConfirmationIsSafe({ ...CONFIRMATION, openTaskCount: 3.5 }, DESCRIPTOR), false);
  assert.equal(seedConfirmationIsSafe({ ...CONFIRMATION, replayed: "false" }, DESCRIPTOR), false);
});


test("confirmation copy distinguishes the installed pack from open work", () => {
  assert.equal(
    syntheticSeedConfirmationMessage({
      ...CONFIRMATION, openTaskCount: 3, replayed: true,
    }),
    "The same 4-case synthetic pack is installed; 3 tasks remain open. No duplicates were added.",
  );
  assert.equal(
    syntheticSeedConfirmationMessage({
      ...CONFIRMATION, openTaskCount: 1, replayed: true,
    }),
    "The same 4-case synthetic pack is installed; 1 task remains open. No duplicates were added.",
  );
});


test("rejects every active outbound latch and any non-record-only transport", () => {
  for (const field of [
    "schedulerEnabled",
    "patientMessagingEnabled",
    "operationallyActivated",
    "automaticContact",
  ]) {
    assert.equal(seedConfirmationIsSafe({
      ...CONFIRMATION,
      outbound: { ...CONFIRMATION.outbound, [field]: true },
    }, DESCRIPTOR), false, field);
  }
  assert.equal(seedConfirmationIsSafe({
    ...CONFIRMATION,
    outbound: { ...CONFIRMATION.outbound, transport: "azure_communication" },
  }, DESCRIPTOR), false);
});


test("uses fixed status-specific errors without reflecting exception details", () => {
  const secret = "patient-name-and-internal-route";
  const expected = new Map([
    [401, "Your session expired. Sign in again before loading synthetic tasks."],
    [403, "Admin access is required to load the synthetic demo tasks."],
    [409, "A safety latch or existing chase state blocked the synthetic demo setup."],
    [422, "The approved synthetic demo request was rejected. Refresh and try again."],
    [503, "Synthetic demo setup is temporarily unavailable. Try again later."],
  ]);
  for (const [status, message] of expected) {
    const rendered = syntheticSeedErrorMessage({ status, message: secret });
    assert.equal(rendered, message);
    assert.equal(rendered.includes(secret), false);
  }
  assert.equal(
    syntheticSeedErrorMessage({ message: secret }),
    "Could not reach synthetic demo setup. Check the connection and try again.",
  );
});
