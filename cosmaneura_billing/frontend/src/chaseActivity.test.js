import assert from "node:assert/strict";
import test from "node:test";
import { chaseAttemptTime, chaseMinutesLabel, chaseOutcomeLabel, readChaseActivity } from "./chaseActivity.js";

const attempt = (overrides = {}) => ({
  at: "2026-09-25T14:00:00+00:00", channel: "phone", performer: "staff",
  outcome: "no_answer", minutes: 2, ...overrides,
});
function payload(history = []) {
  return {
    schemaVersion: "chase-activity.v1", totalAttempts: history.length,
    countsByChannel: Object.fromEntries(["phone", "email", "sms", "portal"].map((channel) =>
      [channel, history.filter((entry) => entry.channel === channel).length])),
    staffMinutes: Math.round(history.reduce((sum, entry) => sum + entry.minutes, 0) * 100) / 100,
    lastAttempt: history[0] || null, history: history.slice(0, 50), truncated: history.length > 50,
  };
}

test("distinguishes a verified empty history from missing or unsupported activity", () => {
  const empty = readChaseActivity(payload());
  assert.equal(empty.totalAttempts, 0);
  assert.equal(empty.lastAttempt, null);
  for (const value of [undefined, null, {}, [], { ...payload(), schemaVersion: "future.v2" }]) {
    assert.equal(readChaseActivity(value), null);
  }
});

test("projects only safe history fields and summarizes mixed channels", () => {
  const result = readChaseActivity(payload([
    attempt({ actor: "private-staff-id", note: "private free text", email: "private@example.com" }),
    attempt({ at: "2026-09-24T12:30:00Z", channel: "email", performer: "system", outcome: "accepted", minutes: 0 }),
    attempt({ at: "2026-09-23T12:30:00Z", channel: "sms", performer: "system", outcome: "sent", minutes: 0 }),
  ]));
  assert.equal(result.totalAttempts, 3);
  assert.deepEqual(result.countsByChannel, { phone: 1, email: 1, sms: 1, portal: 0 });
  assert.equal(result.staffMinutes, 2);
  assert.deepEqual(result.lastAttempt, attempt());
  assert.equal(JSON.stringify(result).includes("private"), false);
  assert.equal(chaseOutcomeLabel("accepted"), "Accepted by provider — delivery unconfirmed");
  assert.equal(chaseOutcomeLabel("sent"), "Sent — delivery unconfirmed");
});

test("rejects inconsistent totals, counts, minutes, last attempt, or ordering", () => {
  const original = payload([attempt(), attempt({ at: "2026-09-24T14:00:00Z" })]);
  for (const change of [
    { totalAttempts: 3 }, { totalAttempts: -1 }, { totalAttempts: 1.5 },
    { countsByChannel: { phone: 1, email: 1, sms: 0, portal: 0 } },
    { countsByChannel: { phone: 2, email: -1, sms: 1, portal: 0 } },
    { countsByChannel: { phone: 2 } }, { staffMinutes: NaN }, { staffMinutes: 1 },
    { staffMinutes: 6 }, { staffMinutes: -1 }, { lastAttempt: null },
    { lastAttempt: attempt({ outcome: "reached" }) }, { history: [...original.history].reverse() },
    { truncated: true }, { truncated: "false" },
  ]) assert.equal(readChaseActivity({ ...original, ...change }), null, JSON.stringify(change));
});

test("bounded history preserves all-time counts and does not deduplicate coincident contacts", () => {
  const all = Array.from({ length: 51 }, () => attempt());
  const result = readChaseActivity(payload(all));
  assert.equal(result.totalAttempts, 51);
  assert.equal(result.history.length, 50);
  assert.equal(result.staffMinutes, 102);
  assert.equal(result.truncated, true);
  assert.equal(readChaseActivity({ ...payload(all), history: all }), null);
  assert.equal(readChaseActivity({ ...payload(all), staffMinutes: 98 }), null);
});

test("invalid timestamps, channels, performers, outcomes, and minute values never reach rendering", () => {
  for (const change of [
    { at: "bad date" }, { at: "2026-09-25T14:00:00" }, { at: "2026-09-25T14:00:00-00:00" }, { at: "2026-02-31T14:00:00Z" },
    { at: "2026-09-25T25:00:00Z" }, { at: "2026-09-25T14:00:00+99:00" },
    { channel: "private-contact" }, { channel: "__proto__" }, { performer: "staff-id" },
    { outcome: "free-text-patient-information" }, { outcome: "__proto__" },
    { minutes: Infinity }, { minutes: "2" }, { minutes: -1 }, { minutes: 1441 },
  ]) assert.equal(readChaseActivity(payload([attempt(change)])), null, JSON.stringify(change));
  assert.equal(chaseAttemptTime("bad date"), "Time unavailable");
  assert.equal(chaseAttemptTime("2026-09-25T14:00:00-00:00"), "Time unavailable");
  assert.equal(chaseMinutesLabel(NaN), "Unavailable");
  assert.equal(chaseOutcomeLabel("__proto__"), "Outcome unavailable");
});

test("formats valid offset instants and fractional minutes without trusting server display text", () => {
  const result = readChaseActivity(payload([attempt({ at: "2026-09-25T14:00:00.123456-04:00", minutes: 2.25 })]));
  assert.ok(result);
  assert.match(chaseAttemptTime(result.lastAttempt.at), /2026/);
  assert.equal(chaseMinutesLabel(2.25), "2.25");
  assert.ok(readChaseActivity({ ...payload([attempt({ minutes: 2.675 })]), staffMinutes: 2.67 }));
});

test("platform attempts preserve delivery, rejection, and investigation distinctions", () => {
  for (const outcome of ["queued", "sending", "delivered", "undelivered", "rejected", "unknown", "failed",
    "ringing", "in-progress", "completed", "busy", "no-answer", "canceled", "confirmed_delivered",
    "confirmed_not_sent", "confirmed_attempted"]) {
    const result = readChaseActivity(payload([attempt({channel: "sms", performer: "system", minutes: 0, outcome})]));
    assert.equal(result.totalAttempts, 1);
    assert.equal(result.lastAttempt.outcome, outcome);
    assert.equal(result.staffMinutes, 0);
  }
  assert.match(chaseOutcomeLabel("queued"), /delivery unconfirmed/);
  assert.equal(chaseOutcomeLabel("completed"), "Call completed");
  assert.notEqual(chaseOutcomeLabel("completed"), chaseOutcomeLabel("delivered"));
  assert.match(chaseOutcomeLabel("rejected"), /Rejected/);
  assert.match(chaseOutcomeLabel("confirmed_not_sent"), /not sent/);
  assert.notEqual(chaseOutcomeLabel("confirmed_delivered"), chaseOutcomeLabel("delivered"));
});
