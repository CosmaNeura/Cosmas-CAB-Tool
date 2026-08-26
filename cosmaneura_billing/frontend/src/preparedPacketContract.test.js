import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";

const CONTRACT = new URL("../../../docs/contracts/prepared-billing-packet/v1/", import.meta.url);
const VECTORS = JSON.parse(readFileSync(new URL("canonicalization-vectors.v1.json", CONTRACT), "utf8"));
const SNAPSHOT = JSON.parse(readFileSync(new URL("examples/review-snapshot.golden.json", CONTRACT), "utf8"));
const PACKET = JSON.parse(readFileSync(new URL("examples/prepared-packet.golden.json", CONTRACT), "utf8"));
const SNAPSHOT_REQUEST = JSON.parse(readFileSync(new URL("examples/review-snapshot-request.golden.json", CONTRACT), "utf8"));
const PREPARE_REQUEST = JSON.parse(readFileSync(new URL("examples/prepare-request.golden.json", CONTRACT), "utf8"));
const MAX_SAFE_INTEGER = 9_007_199_254_740_991;

function assertUnicodeScalarString(value) {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      assert.ok(next >= 0xdc00 && next <= 0xdfff, "unpaired high surrogate");
      index += 1;
    } else {
      assert.ok(unit < 0xdc00 || unit > 0xdfff, "unpaired low surrogate");
    }
  }
}

function canonicalString(value) {
  if (value === null || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "string") {
    assertUnicodeScalarString(value);
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    assert.ok(Number.isSafeInteger(value), "canonical numbers must be safe integers");
    assert.ok(!Object.is(value, -0), "negative zero is prohibited");
    assert.ok(Math.abs(value) <= MAX_SAFE_INTEGER, "unsafe integer");
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalString).join(",")}]`;
  assert.equal(typeof value, "object");
  assert.equal(Object.getPrototypeOf(value), Object.prototype);
  const fields = Object.keys(value).sort().map((key) => {
    assert.match(key, /^[\x00-\x7f]*$/, "canonical keys must be ASCII");
    return `${JSON.stringify(key)}:${canonicalString(value[key])}`;
  });
  return `{${fields.join(",")}}`;
}

function canonicalBytes(value) {
  return Buffer.from(canonicalString(value), "utf8");
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function verifySealed(value, digestField) {
  const body = { ...value };
  const expected = body[digestField];
  delete body[digestField];
  assert.equal(sha256(canonicalBytes(body)), expected);
  return canonicalBytes(value);
}

test("browser canonicalizer matches every language-neutral vector", () => {
  for (const vector of VECTORS.positive) {
    const expected = Buffer.from(vector.canonicalUtf8Base64, "base64");
    assert.deepEqual(canonicalBytes(vector.value), expected, vector.name);
    assert.equal(sha256(expected), vector.sha256, vector.name);
  }
  assert.notEqual(VECTORS.positive[1].sha256, VECTORS.positive[2].sha256);
  assert.notEqual(VECTORS.positive[4].sha256, VECTORS.positive[5].sha256);
});

test("browser digest exclusion removes only the named root field", () => {
  const vector = VECTORS.digestExclusion;
  const body = { ...vector.sealedValue };
  delete body[vector.digestField];
  assert.deepEqual(canonicalBytes(body), Buffer.from(vector.digestInputUtf8Base64, "base64"));
  assert.equal(sha256(canonicalBytes(body)), vector.sha256);
  assert.deepEqual(
    canonicalBytes(vector.sealedValue),
    Buffer.from(vector.sealedCanonicalUtf8Base64, "base64"),
  );
  assert.equal(body.nestedDigest, vector.sealedValue.nestedDigest);
});

test("browser verifies the exact snapshot and prepared packet digests", () => {
  assert.ok(verifySealed(SNAPSHOT, "snapshotDigest").byteLength <= 256 * 1024);
  assert.ok(verifySealed(PACKET, "packetDigest").byteLength <= 512 * 1024);
  assert.deepEqual(PACKET.reviewSnapshot, SNAPSHOT);
  assert.equal(PACKET.status, "prepared_not_transmitted");
  assert.deepEqual(PACKET.transmission, { performed: false, destination: null });
  assert.equal(SNAPSHOT.engineProvenance.adaptivePreferencesApplied, false);
  assert.equal(SNAPSHOT.engineProvenance.multiEngineDecisioningApplied, false);
  assert.equal(Object.hasOwn(PACKET, "replayed"), false);
});

test("browser pins whole-request hashes for exact retry", () => {
  assert.equal(
    sha256(canonicalBytes(SNAPSHOT_REQUEST)),
    VECTORS.requestDigests.reviewSnapshotRequestSha256,
  );
  assert.equal(
    sha256(canonicalBytes(PREPARE_REQUEST)),
    VECTORS.requestDigests.prepareRequestSha256,
  );
});

test("browser uses UTF-8 byte offsets and raw-byte segment digests", () => {
  const source = Buffer.from(VECTORS.evidenceUtf8.sourceUtf8Base64, "base64");
  let previousEnd = -1;
  for (const segment of VECTORS.evidenceUtf8.segments) {
    assert.ok(previousEnd < segment.startByte && segment.startByte < segment.endByte);
    const selected = source.subarray(segment.startByte, segment.endByte);
    assert.equal(selected.toString("utf8"), segment.text);
    assert.equal(sha256(selected), segment.textDigest);
    previousEnd = segment.endByte;
  }
});

test("browser canonicalizer rejects non-canonical JavaScript values", () => {
  for (const value of [1.5, Number.NaN, Number.POSITIVE_INFINITY, -0, MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => canonicalBytes({ value }));
  }
  assert.throws(() => canonicalBytes({ value: "\ud800" }));
  assert.throws(() => canonicalBytes(new Map([["a", 1]])));
});

test("packet canonical JSON remains distinct from fixture-manifest v1", () => {
  const vector = VECTORS.fixtureProtocolSeparation;
  assert.equal(sha256(canonicalBytes(vector.value)), vector.packetCanonicalJsonV1Sha256);
  assert.notEqual(vector.packetCanonicalJsonV1Sha256, vector.fixtureManifestV1Sha256);
});

test("contract artifact path remains outside runtime package data", () => {
  assert.match(fileURLToPath(CONTRACT), /\/docs\/contracts\/prepared-billing-packet\/v1\/$/);
});
