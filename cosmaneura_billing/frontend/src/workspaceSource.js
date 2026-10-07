import { isSyntheticDemoEncounter, isValidEncounterVersion } from "./providerBilling.js";
import { isOperationalHeaderEntry, readLaunchEncounter } from "./providerHeaderLaunch.js";

const SOURCES = new Set(["provider_completed_note", "athena_staff_review_operational"]);
const require = (ok) => { if (!ok) throw new Error("unverified-workspace-source"); };

/** Navigation metadata is not model permission. Detail and every API action reauthorize. */
export function isWorkspaceEncounterEntry(value, headerAvailable = true) {
  return isSyntheticDemoEncounter(value) || (headerAvailable && isOperationalHeaderEntry(value)
    && SOURCES.has(value.ingestionSource));
}

export async function readWorkspaceEncounter(detail, selectedId, listed) {
  require(listed?.id === selectedId && isWorkspaceEncounterEntry(listed)
    && detail?.id === selectedId && detail.dataClass === listed.dataClass
    && detail.readOnly === listed.readOnly && isValidEncounterVersion(detail.encounterVersion));
  if (detail.dataClass === "operational") require(detail.ingestionSource === listed.ingestionSource
    && detail.headerSource?.ingestionSource === detail.ingestionSource);
  if (detail.headerSource || detail.dataClass === "operational") {
    return { ...await readLaunchEncounter(detail, selectedId), submittedEm: detail.submittedEm || null,
      ...(detail.dataClass === "operational" ? { ingestionSource: detail.ingestionSource } : {}) };
  }
  // Backward-compatible synthetic workspace when the header release control is off.
  require(isSyntheticDemoEncounter(detail));
  return { ...detail, encounterId: selectedId, headerSource: null };
}

export const workspaceInferenceAllowed = (encounter) => isSyntheticDemoEncounter(encounter)
  || (SOURCES.has(encounter?.ingestionSource) && encounter.inferencePolicy?.allowed === true
    && encounter.inferencePolicy.reason === null);

export function readWorkspaceResult(result, encounter, idField = "encounterId") {
  require(result?.[idField] === encounter.encounterId && result.encounterVersion === encounter.encounterVersion
    && result.dataClass === encounter.dataClass && result.readOnly === encounter.readOnly);
  if (encounter.dataClass === "operational") {
    require(result.ingestionSource === encounter.ingestionSource && isOperationalHeaderEntry({ ...result, id: encounter.encounterId })
      && workspaceInferenceAllowed(result) && workspaceInferenceAllowed(encounter));
  } else require(isSyntheticDemoEncounter(result));
  return result;
}

/** Read source first so a denied policy never starts a new coder-model request. */
export async function loadCoderWorkspace(encounterId, listed, request, onVerifiedEncounter = () => {}) {
  const detail = await request("/api/encounters/detail", { encounterId });
  const encounter = await readWorkspaceEncounter(detail, encounterId, listed);
  onVerifiedEncounter(encounter);
  if (!workspaceInferenceAllowed(encounter)) return { encounter, review: null };
  const review = await request("/api/encounters/coder-view", { encounterId });
  return { encounter, review: readWorkspaceResult(review, encounter) };
}
