import { isSyntheticDemoEncounter, isValidEncounterVersion } from "./providerBilling.js";
import { boundSourceVisitContext } from "./sourceVisitContext.js";

const encounterId = (value) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const require = (condition) => { if (!condition) throw new Error("unverified-launch-source"); };

/** A route selector only. The server still authorizes the ID and every action. */
export function headerLaunchRequest(search) {
  const params = new URLSearchParams(search);
  if (!params.getAll("view").includes("header")) return null;
  if (params.getAll("view").length !== 1 || params.getAll("encounterId").length !== 1
    || [...params.keys()].some((key) => !["view", "encounterId"].includes(key))
    || !encounterId(params.get("encounterId"))) return { error: "This billing-header link is incomplete or invalid. Open it again from the selected encounter." };
  return { encounterId: params.get("encounterId") };
}

export function headerLaunchPath(id) {
  if (!encounterId(id)) throw new Error("invalid-launch-id");
  return `/?view=header&encounterId=${encodeURIComponent(id)}`;
}
export function headerLaunchSignIn(id) {
  return `/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(headerLaunchPath(id))}`;
}

export function canLaunchHeader(session) {
  return Array.isArray(session?.views) && session.views.includes("provider")
    && session.capabilities?.billingHeader === true;
}

const inferencePolicyValid = (value) => !!value && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).sort().join(",") === "allowed,reason"
  && (value.allowed === true ? value.reason === null
    : value.allowed === false && value.reason === "operational_source_model_not_approved");

/** A listed navigation candidate, not model authorization. Detail and generation recheck on the server. */
export const isOperationalHeaderEntry = (value) => encounterId(value?.id) && value.dataClass === "operational"
  && value.readOnly === false && inferencePolicyValid(value.inferencePolicy);

async function sourceHash(text) {
  const bytes = new TextEncoder().encode(text);
  return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), (n) => n.toString(16).padStart(2, "0")).join("");
}

export async function readLaunchEncounter(detail, selectedId) {
  const operationalAthena = detail?.dataClass === "operational" && detail.readOnly === false
    && detail.headerSource?.ingestionSource === "athena_staff_review_operational";
  if (operationalAthena) require(inferencePolicyValid(detail.inferencePolicy));
  require(detail?.id === selectedId && (isSyntheticDemoEncounter(detail) || operationalAthena)
    && isValidEncounterVersion(detail.encounterVersion));
  const pin = detail.headerSource;
  require(pin && pin.encounterId === selectedId && pin.encounterVersion === detail.encounterVersion
    && (operationalAthena || pin.ingestionSource === detail.dataClass) && typeof pin.noteId === "string" && pin.noteId.length > 0
    && pin.noteVersion === String(detail.encounterVersion) && hash(pin.noteSha256) && hash(pin.bundleSha256)
    && Array.isArray(pin.supportingSourceDigests) && pin.supportingSourceDigests.length <= 2);
  for (const field of ["signedNote", "transcript", "intake"]) {
    require(typeof detail[field] === "string" && [...detail[field]].length <= 200_000);
  }
  require(await sourceHash(detail.signedNote) === pin.noteSha256);
  const kinds = new Set(), ids = new Set([pin.noteId]);
  for (const doc of pin.supportingSourceDigests) {
    require(["transcript", "intake"].includes(doc.kind) && !kinds.has(doc.kind)
      && typeof doc.id === "string" && doc.id.length > 0 && !ids.has(doc.id)
      && doc.version === String(detail.encounterVersion) && hash(doc.sha256));
    kinds.add(doc.kind); ids.add(doc.id);
    require(await sourceHash(detail[doc.kind]) === doc.sha256);
  }
  require(["transcript", "intake"].every((kind) => !detail[kind] || kinds.has(kind)));
  require(typeof detail.encounterType === "string" && typeof detail.placeOfService === "string");
  return { encounterId: selectedId, encounterVersion: detail.encounterVersion, headerSource: pin,
    intake: detail.intake, transcript: detail.transcript, signedNote: detail.signedNote,
    encounterType: detail.encounterType, placeOfService: detail.placeOfService,
    sourceVisitContext: boundSourceVisitContext(detail.sourceVisitContext, pin),
    dataClass: detail.dataClass, readOnly: detail.readOnly,
    ...(operationalAthena ? { inferencePolicy: { ...detail.inferencePolicy } } : {}) };
}
