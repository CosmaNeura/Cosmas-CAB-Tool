const hash = (v) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const uuid = (v) => typeof v === "string" && /^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/.test(v);
const text = (v, max, blank = false) => typeof v === "string" && v.length <= max
  && (blank || !!v.trim()) && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(v)
  && !/[\uD800-\uDFFF]/u.test(v);
const list = (v, max) => Array.isArray(v) && v.length <= max;
const object = (v) => v && typeof v === "object" && !Array.isArray(v);
const sourcePatient = (v) => typeof v === "string" && /^601(?:7[89]|8[0-4])$/.test(v);
const sourceId = (v) => typeof v === "string" && /^[1-9][0-9]{0,17}$/.test(v);
const require = (v) => { if (!v) throw new Error("invalid-athena-response"); };
const fieldValid = (f) => object(f) && text(f.name, 80)
  && ["absent", "null", "empty", "invalid", "present"].includes(f.state)
  && (f.value === null || text(f.value, 2048, true) || typeof f.value === "boolean" || Number.isSafeInteger(f.value))
  && text(f.source?.pointer, 512) && hash(f.source?.sha256);
const fieldsValid = (v) => list(v, 40) && v.every(fieldValid) && new Set(v.map((f) => f.name)).size === v.length;
const policyValid = (p) => object(p) && hash(p.candidateId) && fieldsValid(p.fields)
  && list(p.issues, 50) && p.issues.every((s) => text(s, 100))
  && p.subscriberPatientMatch === "unverified" && p.eligibilityVerifiedByCab === false;
const policiesValid = (p) => list(p, 15) && p.every(policyValid)
  && new Set(p.map((v) => v.candidateId)).size === p.length;
const time = (v) => text(v, 40) && /^\d{4}-\d{2}-\d{2}T/.test(v) && Number.isFinite(Date.parse(v));
const token = (v) => v === null || text(v, 12288);
const same = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
const canonical = (v) => Array.isArray(v) ? v.map(canonical) : object(v)
  ? Object.fromEntries(Object.keys(v).sort().map((key) => [key, canonical(v[key])])) : v;
const departmentValid = (field) => object(field) && (field.state === "present" ? sourceId(field.value)
  : field.state === "absent" && field.value === null);
const departmentProvenanceValid = (value) => value.sourceDepartmentOrigin === "not_supplied"
  ? value.sourceDepartmentId === null
  : ["list_and_metadata", "metadata"].includes(value.sourceDepartmentOrigin) && sourceId(value.sourceDepartmentId);
const hasDepartmentProvenance = (value) => Object.hasOwn(value, "sourceDepartmentId") || Object.hasOwn(value, "sourceDepartmentOrigin");
export const athenaSourceDepartment = (value) => sourceId(value) ? value : "Not supplied";
const discoveryRequire = (valid) => {
  if (!valid) throw Object.assign(new Error("invalid-athena-discovery"), { status: 409 });
};

const closed = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const identifier = (value) => text(value, 128) && value.trim() === value && !/[\r\n\t]/.test(value);
const revision = (value) => Number.isSafeInteger(value) && value > 0 && value <= 2147483647;
const bindingKeys = ["schemaVersion", "serverPracticeId", "chaseId", "environment", "vendorPracticeId", "sourcePatientId", "connectionRevision", "bindingRevision", "profileSha256", "scopeSha256"];
const bindingValid = (value, chaseId) => closed(value, bindingKeys) && value.schemaVersion === "athena-source-binding.v1"
  && identifier(value.serverPracticeId) && value.chaseId === chaseId && identifier(value.chaseId)
  && ["preview", "production"].includes(value.environment) && sourceId(value.vendorPracticeId) && sourceId(value.sourcePatientId)
  && revision(value.connectionRevision) && revision(value.bindingRevision) && hash(value.profileSha256) && hash(value.scopeSha256);
// Stored identities and expired proofs remain readable history. The server's
// current read state and exact grant CAS separately authorize new source work.
const admittedPreviewBinding = (value, chaseId) => bindingValid(value, chaseId) && value.environment === "preview"
  && value.vendorPracticeId === "195900" && sourcePatient(value.sourcePatientId);
const admissionValid = (value, binding) => closed(value, ["schemaVersion", "admissionId", "admissionRevision", "scopeSha256", "grantSha256", "expiresAt"])
  && value.schemaVersion === "athena-read-storage-admission.v1" && uuid(value.admissionId) && revision(value.admissionRevision)
  && hash(value.scopeSha256) && value.scopeSha256 === binding?.scopeSha256 && hash(value.grantSha256)
  && time(value.expiresAt) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value.expiresAt);
const sourceProvenanceValid = (value, chaseId) => bindingValid(value.sourceBinding, chaseId)
  && (value.sourceBinding.environment === "production" ? admissionValid(value.sourceAdmission, value.sourceBinding)
    : admittedPreviewBinding(value.sourceBinding, chaseId) && !Object.hasOwn(value, "sourceAdmission"));
const configuredView = (value) => ["athena-case-review.v2", "athena-case-review.v3"].includes(value.schemaVersion);
const productionBinding = (value) => value?.environment === "production";
export const athenaImportMessage = (value) => value?.source === "athena_staff_review_operational"
  ? "Note imported. Open the encounter to check coding availability."
  : "Reviewed note imported into CAB.";
const connectionStates = ["ready", "changed", "connection_missing", "connection_revoked", "binding_missing", "binding_revoked", "binding_stale", "read_not_admitted", "credential_environment_mismatch"];
const actionFor = { "/api/athena/card-chase/read": "athena.refresh", "/api/athena/card-chase/apply": "athena.apply", "/api/athena/billing/apply": "athena.billing_apply" };

export function readAthenaEnvelope(result, chaseId, command = null, expectedConfigured = false) {
  const view = readAthenaView(result, chaseId), connection = result.connection ?? null, receipt = result.operationReceipt ?? null;
  if (connection === null) { require(!expectedConfigured && !configuredView(view) && receipt === null); return { view, connection, receipt }; }
  const operational = productionBinding(connection.binding);
  require(closed(connection, ["workflow", "state", "readAllowed", "currentReviewMatchesBinding", "binding", ...(operational ? ["admission"] : [])])
    && connection.workflow === "configured" && connectionStates.includes(connection.state)
    && typeof connection.readAllowed === "boolean" && typeof connection.currentReviewMatchesBinding === "boolean"
    && (connection.binding === null || bindingValid(connection.binding, chaseId))
    && (!operational || connection.admission === null || admissionValid(connection.admission, connection.binding)));
  require(connection.readAllowed === ["ready", "changed"].includes(connection.state)
    && (!connection.readAllowed || (operational ? admissionValid(connection.admission, connection.binding) : admittedPreviewBinding(connection.binding, chaseId)))
    && (connection.state !== "changed" || !connection.currentReviewMatchesBinding));
  if (view.sourceBinding && connection.binding) require(view.sourceBinding.serverPracticeId === connection.binding.serverPracticeId);
  if (connection.currentReviewMatchesBinding) require(!view.redacted && configuredView(view)
    && same(view.sourceBinding, connection.binding) && (!operational || same(view.sourceAdmission, connection.admission)));
  if (receipt !== null) {
    require(command && closed(receipt, ["operationId", "revision", "action", "replayed", "current", "sourceBinding", ...(productionBinding(receipt.sourceBinding) ? ["sourceAdmission"] : [])])
      && receipt.operationId === command.body?.operationId && uuid(receipt.operationId) && receipt.action === actionFor[command.path]
      && revision(receipt.revision) && receipt.revision <= view.revision && typeof receipt.replayed === "boolean"
      && typeof receipt.current === "boolean" && receipt.current === (receipt.revision === view.revision)
      && (receipt.sourceBinding === null || sourceProvenanceValid(receipt, chaseId)));
    if (receipt.current && view.sourceBinding) require(same(receipt.sourceBinding, view.sourceBinding)
      && same(receipt.sourceAdmission, view.sourceAdmission));
  } else require(command === null);
  return { view, connection, receipt };
}

export function athenaSourceReadFields(connection, patient) {
  if (!connection) { require(sourcePatient(patient)); return { sourcePatientId: patient }; }
  const operational = productionBinding(connection.binding);
  require(connection.workflow === "configured" && connection.readAllowed === true && ["ready", "changed"].includes(connection.state)
    && bindingValid(connection.binding, connection.binding?.chaseId)
    && (operational ? admissionValid(connection.admission, connection.binding) : admittedPreviewBinding(connection.binding, connection.binding.chaseId)));
  return { expectedBindingSha256: connection.binding.scopeSha256,
    ...(operational ? { expectedAdmissionSha256: connection.admission.grantSha256 } : {}) };
}

export function athenaConnectionMessage(connection, redacted = false) {
  if (redacted) return "The case patient changed. Read the mapped source again before reviewing or importing.";
  return ({ ready: "This case uses its saved Athena patient mapping. Compare the returned identity before applying fields.",
    changed: "The saved Athena mapping or source approval changed. Read the mapped source again before applying fields or importing a note.",
    connection_missing: "No Athena connection is configured for this practice.", connection_revoked: "This practice’s Athena connection was revoked. Source reads are unavailable.",
    binding_missing: "No Athena patient mapping is configured for this case.", binding_revoked: "This case’s Athena patient mapping was revoked. Source reads are unavailable.",
    binding_stale: "This case’s Athena patient mapping needs to be updated before source reads can resume.",
    read_not_admitted: "Athena reads aren’t enabled for this practice yet. Contact an administrator.",
    credential_environment_mismatch: "The Athena connection needs an administrator’s attention before source reads can resume." })[connection?.state] || "";
}

export function readAthenaView(result, chaseId) {
  const v = result?.view;
  require(object(v) && ["athena-case-review.v1", "athena-case-review.v2", "athena-case-review.v3"].includes(v.schemaVersion) && v.chaseId === chaseId
    && Number.isSafeInteger(v.revision) && v.revision >= 0 && v.revision <= 10000);
  if (v.redacted === true) {
    require(closed(v, ["schemaVersion", "chaseId", "revision", "reviewId", "candidate", "applied", "billingImport", "redacted"])
      && configuredView(v) && v.reviewId === null && v.candidate === null && v.applied === null && v.billingImport === null);
    return v;
  }
  const operational = v.schemaVersion === "athena-case-review.v3";
  if (configuredView(v)) require(v.revision > 0 && sourceProvenanceValid(v, chaseId)
    && v.sourceBinding.environment === (operational ? "production" : "preview") && v.mode === v.sourceBinding.environment);
  else require(!Object.hasOwn(v, "sourceAdmission"));
  if (v.revision === 0) { require(v.reviewId === null && v.candidate === null && v.applied === null); return v; }
  const c = v.candidate;
  require(uuid(v.reviewId) && (operational ? v.mode === "production" : ["mock", "preview"].includes(v.mode)) && hash(v.caseBinding)
    && c?.schemaVersion === (operational ? "athena-card-chase-review.v2" : "athena-card-chase-review.v1") && hash(c.candidateId) && hash(c.sourceDigest)
    && (operational ? c.source?.system === "athena_production" && c.source.syntheticOnly === false && c.source.dataClass === "operational"
      : sourcePatient(c.source?.patientId) && c.source?.vendorPracticeId === "195900" && c.source?.syntheticOnly === true && !Object.hasOwn(c, "sourceAdmission"))
    && time(c.source?.observedAt)
    && fieldsValid(c.patient?.fields) && policiesValid(c.insurance?.policies));
  if (configuredView(v)) require(same(c.sourceBinding, v.sourceBinding) && same(c.sourceAdmission, v.sourceAdmission)
    && c.source.patientId === v.sourceBinding.sourcePatientId && c.source.vendorPracticeId === v.sourceBinding.vendorPracticeId);
  for (const key of ["patient", "insurance"]) {
    require(["complete", "partial", "empty", "unavailable"].includes(c.reads?.[key]?.state));
  }
  require(list(c.contact?.candidates, 3) && c.contact.outreachAuthorized === false
    && Object.values(c.contact.effectiveConsent || {}).length === 3
    && Object.values(c.contact.effectiveConsent).every((s) => s === "unknown"));
  require(c.contact.candidates.every((r) => ["mobile", "home", "email"].includes(r.kind)
    && (r.field === null || fieldValid(r.field)) && text(r.formatStatus, 80)
    && r.usableForOutreach === false));
  if (v.applied !== null) {
    const a = v.applied;
    if (a.sourceBinding !== undefined) require(sourceProvenanceValid(a, chaseId)
      && (!v.sourceBinding || a.sourceBinding.serverPracticeId === v.sourceBinding.serverPracticeId));
    else require(!Object.hasOwn(a, "sourceAdmission"));
    if (v.sourceBinding && a.reviewId === v.reviewId) require(same(a.sourceBinding, v.sourceBinding) && same(a.sourceAdmission, v.sourceAdmission));
    require(uuid(a.reviewId) && uuid(a.operationId) && hash(a.sourceDigest)
      && fieldsValid(a.patient?.fields) && policiesValid(a.policies) && time(a.at)
      && a.consent === "unknown" && a.eligibilityVerified === false && a.caseClosed === false
      && list(a.contacts, 2) && a.contacts.every((r) => ["mobile_phone", "home_phone", "email"].includes(r.field)
        && text(r.value, 512) && r.consent === "unknown"));
    require(a.contacts.every((r) => r.normalization == null || (r.field !== "email"
      && r.normalization.country === "US" && r.normalization.confirmedByStaff === true
      && nationalPhoneDigits(r.normalization.sourceValue) !== null
      && r.value === `+1${nationalPhoneDigits(r.normalization.sourceValue)}`
      && athenaField(a.patient.fields, r.field)?.value === r.normalization.sourceValue)));
  }
  if (v.billingImport != null) {
    const b = v.billingImport;
    const operationalImport = b.source === "athena_staff_review_operational";
    if (b.sourceBinding !== undefined) require(sourceProvenanceValid(b, chaseId)
      && (!v.sourceBinding || b.sourceBinding.serverPracticeId === v.sourceBinding.serverPracticeId)
      && b.sourceBinding.sourcePatientId === b.sourcePatientId && productionBinding(b.sourceBinding) === operationalImport);
    else require(!operationalImport && !Object.hasOwn(b, "sourceAdmission"));
    require(text(b.encounterId, 128) && /^athena-review-[a-f0-9-]{36}$/.test(b.encounterId)
      && ["athena_staff_review_mock", "athena_staff_review_preview_synthetic", "athena_staff_review_operational"].includes(b.source)
      && (operationalImport ? sourceId(b.sourcePatientId) : sourcePatient(b.sourcePatientId)) && hash(b.sourceNoteSha256) && hash(b.sourceMetadataSha256)
      && hash(b.plainTextSha256) && time(b.at));
    if (hasDepartmentProvenance(b) || Object.hasOwn(b, "departmentIdMeaning")) {
      require(b.departmentIdMeaning === "chart_query" && sourceId(b.departmentId)
        && departmentProvenanceValid(b));
    }
  }
  return v;
}

export function readAthenaBilling(result, view, department, encounter = null) {
  require(object(result) && result.schemaVersion === "athena-billing-read.v1"
    && result.sourcePatientId === view.candidate.source.patientId && result.departmentId === department
    && result.caseRevision === view.revision && result.mode === view.mode && result.readyForHeader === false
    && ["complete", "partial", "empty", "unavailable"].includes(result.state)
    && list(result.encounters, 15) && list(result.pageHashes, 3) && result.pageHashes.every(hash));
  if (view.sourceBinding) require(sourceProvenanceValid(result, view.chaseId) && same(result.sourceBinding, view.sourceBinding)
    && same(result.sourceAdmission, view.sourceAdmission));
  else require(!Object.hasOwn(result, "sourceBinding") && !Object.hasOwn(result, "sourceAdmission"));
  require(!Object.hasOwn(result, "departmentIdMeaning") || result.departmentIdMeaning === "chart_query");
  require(result.encounters.every((row) => object(row) && sourceId(row.encounterid?.value)
    && row.encounterid.state === "present" && departmentValid(row.departmentid)
    && Object.values(row).every((f) => object(f) && ["absent", "null", "invalid", "present"].includes(f.state)
      && (f.value === null || text(f.value, 8192, true)))));
  require(new Set(result.encounters.map((row) => row.encounterid.value)).size === result.encounters.length);
  if (Object.hasOwn(result, "pagination")) {
    const p = result.pagination;
    require(object(p) && token(p.nextToken) && token(p.selectionToken)
      && Number.isSafeInteger(p.loadedCount) && p.loadedCount >= result.encounters.length && p.loadedCount <= 150
      && Number.isSafeInteger(p.windowStart) && p.windowStart >= 0
      && typeof p.append === "boolean" && typeof p.nextStartsWindow === "boolean"
      && (!p.nextToken || !!p.selectionToken) && (!p.append || result.encounters.length <= 10));
  }
  if (encounter === null) require(result.note === null);
  else {
    const n = result.note, selected = result.encounters.find((row) => row.encounterid.value === encounter);
    require(n?.encounterId === encounter && result.encounters.some((r) => r.encounterid.value === encounter)
      && text(n.plainText, 131072, true) && hash(n.responseSha256) && hash(n.metadataSha256)
      && n.transformVersion === "athena-summary-text.v1" && n.offsetUnit === "unicode_code_point"
      && n.requiresProviderConfirmation === true && n.sourceAtomicity === "unverified"
      && n.signatureInterpretation === "unverified");
    const listed = selected.departmentid, metadata = n.metadata?.departmentid;
    require(n.metadata?.encounterid?.state === "present" && n.metadata.encounterid.value === encounter
      && departmentValid(metadata) && (listed.state === "absent" || same(listed, metadata)));
    if (hasDepartmentProvenance(n)) {
      const origin = listed.state === "present" ? "list_and_metadata" : metadata.state === "present" ? "metadata" : "not_supplied";
      require(departmentProvenanceValid(n) && n.sourceDepartmentId === metadata.value && n.sourceDepartmentOrigin === origin);
    }
  }
  return result;
}

const discoveryContext = (view, department) => [view.chaseId, view.caseBinding, view.reviewId, view.revision,
  view.mode, view.candidate.source.vendorPracticeId, view.candidate.source.patientId,
  view.candidate.sourceDigest, view.applied?.reviewId, department, view.sourceBinding?.scopeSha256 ?? null, view.sourceAdmission?.grantSha256 ?? null];

export function athenaBillingReadBody(chaseId, department, billing, { encounter = null, continuation = false } = {}) {
  require(!continuation || encounter === null);
  if (encounter !== null) discoveryRequire(billing?.encounters.some((row) => row.encounterid.value === encounter));
  const receipt = continuation ? billing?.pagination?.nextToken : encounter !== null ? billing?.pagination?.selectionToken : null;
  if (continuation) discoveryRequire(!!receipt);
  return { chaseId, departmentId: department, sourceEncounterId: encounter, ...(receipt ? { discoveryToken: receipt } : {}) };
}

// Validate the bounded response before replacing any visible state. The local
// context and last response receipt also bind merges and exact request replay.
export function mergeAthenaBilling(previous, result, view, department,
  { encounter = null, continuation = false, discoveryToken = null } = {}) {
  require(!continuation || encounter === null);
  const context = discoveryContext(view, department);
  if (continuation || encounter !== null) {
    discoveryRequire(previous && same(previous.discoveryContext, context)
      && result?.sourcePatientId === previous.sourcePatientId && result?.departmentId === previous.departmentId
      && result?.caseRevision === previous.caseRevision && result?.mode === previous.mode);
  }
  const next = readAthenaBilling(result, view, department, encounter), p = next.pagination;
  if (!continuation && encounter === null) {
    require(discoveryToken === null && (!p || (!p.append && p.windowStart === 0 && p.loadedCount === next.encounters.length)));
    return { ...next, discoveryContext: context };
  }
  const old = previous.pagination;
  discoveryRequire(!!p === !!old);
  if (encounter !== null) {
    discoveryRequire(discoveryToken === (old?.selectionToken ?? null));
    if (p) discoveryRequire(!p.append && p.windowStart === old.windowStart && p.loadedCount === old.loadedCount
      && p.selectionToken === old.selectionToken && p.nextToken === old.nextToken
      && p.nextStartsWindow === old.nextStartsWindow && (!discoveryToken || next.encounters.length <= 5));
    discoveryRequire(next.encounters.every((row) => previous.encounters.some((saved) => same(saved, row))));
    return { ...previous, note: next.note };
  }
  const last = previous.lastDiscoveryPage;
  if (last?.discoveryToken === discoveryToken && same(last.response, next)) return previous;
  discoveryRequire(p && old && discoveryToken !== null && discoveryToken === old.nextToken);
  const existing = new Set(previous.encounters.map((row) => row.encounterid.value));
  discoveryRequire(next.encounters.every((row) => !existing.has(row.encounterid.value)));
  if (old.nextStartsWindow) {
    discoveryRequire(!p.append && p.windowStart === old.windowStart + old.loadedCount
      && p.loadedCount === next.encounters.length && next.encounters.length <= 10);
  } else {
    discoveryRequire(p.append && p.windowStart === old.windowStart
      && p.loadedCount === previous.encounters.length + next.encounters.length);
  }
  const pageHashes = p.append ? [...previous.pageHashes, ...next.pageHashes] : next.pageHashes;
  discoveryRequire(pageHashes.length <= 30);
  return { ...next, discoveryContext: context,
    encounters: p.append ? [...previous.encounters, ...next.encounters] : next.encounters,
    pageHashes, note: p.append ? previous.note : null, lastDiscoveryPage: { discoveryToken, response: next } };
}

export const athenaField = (fields, name) => fields?.find((f) => f.name === name);
function nationalPhoneDigits(value) {
  if (typeof value !== "string") return null;
  const match = value.match(/^(?:[2-9][0-9]{2}|\([2-9][0-9]{2}\))[-. ]?[2-9][0-9]{2}[-. ]?[0-9]{4}$/);
  return match && match[0] === value ? value.replace(/[(). -]/g, "") : null;
}
export function athenaPhoneReview(contact, country = "") {
  const unavailable = { selectable: false, needsCountry: false, normalizedValue: null };
  if (!["mobile", "home"].includes(contact?.kind) || contact.field?.state !== "present") return unavailable;
  const value = contact.field.value;
  if (contact.formatStatus === "plausible" && typeof value === "string"
    && value.trim() === value && /^\+[1-9][0-9]{7,14}$/.test(value)) {
    return { selectable: true, needsCountry: false, normalizedValue: country ? null : value };
  }
  const digits = nationalPhoneDigits(value);
  if (contact.formatStatus !== "country_confirmation_required" || !digits) return unavailable;
  return { selectable: true, needsCountry: true, normalizedValue: country === "US" ? `+1${digits}` : null };
}
export function athenaDepartments(fields) {
  const choices = new Map();
  for (const [name, label] of [["current_department_id", "Current"], ["registration_department_id", "Registration"]]) {
    const field = athenaField(fields, name);
    if (field?.state !== "present" || !sourceId(field.value)) continue;
    const existing = choices.get(field.value);
    if (existing) existing.labels.push(label.toLowerCase());
    else choices.set(field.value, { id: field.value, labels: [label] });
  }
  return [...choices.values()].map(({ id, labels }) => ({ id, label: `${labels.join(" / ")} department · ${id}` }));
}
export function fieldDisplay(field) {
  if (!field) return "Not supplied";
  if (field.state !== "present") return field.state === "invalid" ? "Unrecognized source value" : "Not supplied";
  return typeof field.value === "boolean" ? (field.value ? "True (source observation)" : "False (source observation)") : String(field.value);
}
export const ambiguousAthenaFailure = (error) => !error?.status || error.status >= 500 || error.status === 429;
export function athenaFailure(error, { kind = "change", timedOut = false, continuation = false } = {}) {
  const readOnly = kind === "billing" || kind === "snapshot";
  const readAgain = kind === "billing" ? continuation
    ? "The loaded encounters and selected note are unchanged. Select Load more encounters or Load older encounters to try again."
    : "Select Find encounters to read the source again."
    : "Select Refresh case review to try again.";
  if (timedOut) return readOnly ? `The source request timed out. ${readAgain}`
    : "The request timed out. Its result is uncertain; retry the same request before starting another change.";
  if (error?.status === 409 && kind === "billing") return "The source or review changed. Select Find encounters to start a new search before loading, previewing, or importing. If the case review changed, select Refresh case review first. Previous results remain visible.";
  if (error?.status === 409) return readOnly
    ? "The source or review changed. Refresh this case before reading the source again."
    : "The source or review changed. Refresh this case and review the current values before applying.";
  if (error?.status === 403) return readOnly ? "This account cannot access this case's Athena data."
    : "This account cannot access this case or apply Athena data.";
  if (error?.status === 404) return "Athena review is unavailable for this case.";
  if (error?.status === 422) return readOnly ? `Check the selected source and department. ${readAgain}`
    : "Check the selected source and required confirmations.";
  if (readOnly) return `${kind === "billing" ? "The source could not be read." : "The case review could not be loaded."} ${readAgain}`;
  return "The request could not be confirmed. Retry the same request before starting another change.";
}
