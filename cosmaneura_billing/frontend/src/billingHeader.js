// Validate the whole header against the currently displayed source before render.
export const diagnosisOrderDecisionId = "@diagnosis-order";
export const factKeys = ["visit_type", "patient_type", "modality", "patient_location",
  "place_of_service", "provider_location", "consent", "visit_duration", "em_time"];
const canonical = (v) => JSON.stringify(v, (_, value) => value && !Array.isArray(value)
  && typeof value === "object" ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);
const same = (a, b) => canonical(a) === canonical(b);
const text = (v, max, blank = false) => typeof v === "string" && v.length <= max
  && (blank || v.trim().length > 0);
const list = (v, max) => Array.isArray(v) && v.length <= max;
const id = (v) => text(v, 160) && /^[A-Za-z0-9._:~-]+$/.test(v);
const uuid = (v) => typeof v === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
function require(ok) { if (!ok) throw new Error("invalid-header-response"); }
const actorValid = (actor) => actor && uuid(actor.id) && text(actor.name, 100)
  && ["provider", "coder", "admin"].includes(actor.role);
const timeValid = (v) => text(v, 40) && Number.isFinite(Date.parse(v));

export function readBillingHeader(view, encounter) {
  if (view === null) return null;
  const h = view?.header, pin = encounter.headerSource;
  require(h?.schemaVersion === "provider-billing-header.v1" && uuid(h.headerId)
    && h.reviewStatus === "unreviewed" && same(h.expectedSource, pin)
    && pin.encounterId === encounter.encounterId && pin.encounterVersion === encounter.encounterVersion
    && actorValid(h.createdBy) && timeValid(h.generatedAt));
  const docs = new Map([[pin.noteId, {kind: "note", version: pin.noteVersion, text: encounter.signedNote}]]);
  for (const doc of pin.supportingSourceDigests) {
    docs.set(doc.id, {kind: doc.kind, version: doc.version,
      text: doc.kind === "transcript" ? encounter.transcript : encounter.intake});
  }
  require(list(h.evidence, 80));
  const evidence = new Set();
  for (const e of h.evidence) {
    const doc = docs.get(e.sourceId);
    require(id(e.id) && !evidence.has(e.id) && doc && e.source === doc.kind
      && e.sourceVersion === doc.version && e.coordinateSystem === "unicode_code_point"
      && Number.isInteger(e.start) && Number.isInteger(e.end) && e.start >= 0 && e.start < e.end
      && e.end <= Array.from(doc.text).length && text(e.text, 2000)
      && Array.from(doc.text).slice(e.start, e.end).join("") === e.text);
    evidence.add(e.id);
  }
  const refs = (v, min = 1) => list(v, 80) && v.length >= min
    && new Set(v).size === v.length && v.every((r) => evidence.has(r));
  require(list(h.facts, 9) && h.facts.length === factKeys.length);
  const keys = new Set();
  for (const fact of h.facts) {
    require(factKeys.includes(fact.key) && !keys.has(fact.key) && text(fact.label, 100)
      && ["documented", "not_documented", "conflicting"].includes(fact.status));
    keys.add(fact.key);
    require(fact.status === "not_documented" ? fact.value === null && refs(fact.evidenceIds, 0) && fact.evidenceIds.length === 0
      : text(fact.value, 600) && refs(fact.evidenceIds, fact.status === "conflicting" ? 2 : 1));
  }
  const codeValid = (line) => id(line.id) && ["EM", "ICD10"].includes(line.kind)
    && text(line.code, 15) && /^[A-Z0-9][A-Z0-9.]*$/.test(line.code)
    && text(line.label, 150) && refs(line.evidenceIds);
  require(list(h.suggestions, 20) && h.suggestions.every((line) => codeValid(line)
    && line.reviewStatus === "unreviewed" && text(line.basis, 700))
    && new Set(h.suggestions.map((s) => s.id)).size === h.suggestions.length);
  require(list(h.flags, 30) && h.flags.every((f) => id(f.id) && text(f.message, 700)
    && (!Object.hasOwn(f, "evidenceIds") || refs(f.evidenceIds)))
    && h.policy?.status === "not_verified" && text(h.policy.message, 700)
    && text(h.generation?.inferenceMode, 80) && uuid(h.generation?.runId));
  require(Number.isInteger(view.revisionNumber) && view.revisionNumber >= 0 && view.revisionNumber <= 100);
  if (view.revisionNumber === 0) require(view.revision === null);
  else {
    const revision = view.revision;
    require(revision?.number === view.revisionNumber && revision.headerId === h.headerId
      && same(revision.expectedSource, pin) && revision.reviewStatus === "unreviewed"
      && actorValid(revision.actor) && timeValid(revision.createdAt) && text(revision.reason, 600));
    const draft = revision.draft;
    require(draft && same(Object.keys(draft).sort(), ["codes", "facts", "note",
        ...(Object.hasOwn(draft, "diagnosisOrderConfirmed") ? ["diagnosisOrderConfirmed"] : [])].sort())
      && (!Object.hasOwn(draft, "diagnosisOrderConfirmed") || typeof draft.diagnosisOrderConfirmed === "boolean")
      && (draft.diagnosisOrderConfirmed !== true || draft.codes?.some((row) => row.kind === "ICD10"))
      && same(Object.keys(draft.facts).sort(), [...factKeys].sort())
      && Object.values(draft.facts).every((v) => text(v, 600, true))
      && list(draft.codes, 20) && draft.codes.every(codeValid) && text(draft.note, 2000, true)
      && new Set(draft.codes.map((row) => row.id)).size === draft.codes.length
      && new Set(draft.codes.map((row) => `${row.kind}:${row.code}`)).size === draft.codes.length);
    require(list(revision.history, 100) && revision.history.length === revision.number);
    for (const item of revision.history) {
      require(actorValid(item.actor) && timeValid(item.createdAt) && text(item.reason, 600)
        && list(item.changes, 51) && item.changes.every((c) => text(c.path, 180)
          && text(c.label, 150) && JSON.stringify(c.before)?.length <= 10000 && JSON.stringify(c.after)?.length <= 10000));
    }
  }
  if (view.signoff != null) {
    const receipt = view.signoff;
    require(uuid(receipt.id) && uuid(receipt.reviewId) && receipt.headerId === h.headerId
      && receipt.headerRevision === view.revisionNumber && same(receipt.expectedSource, pin)
      && actorValid(receipt.actor) && ["coder", "admin"].includes(receipt.actor.role)
      && receipt.status === "recorded" && timeValid(receipt.createdAt)
      && /^[a-f0-9]{64}$/.test(receipt.recommendationDigest)
      && list(receipt.humanProposalDecisions, 41) && list(receipt.codeDecisions, 500)
      && text(receipt.reviewerNote, 4000, true));
    const proposals = new Set(headerDecisionIds(view));
    require(receipt.humanProposalDecisions.length === proposals.size);
    for (const decision of receipt.humanProposalDecisions) {
      require(proposals.delete(decision.id) && ["accept", "reject"].includes(decision.decision)
        && text(decision.reason, 1000));
    }
    require(receipt.codeDecisions.every((d) => ["EM", "ICD10", "PROCEDURE", "MODIFIER"].includes(d.kind)
      && ["accept", "modify", "reject", "add"].includes(d.action)
      && (d.engine_code === null || text(d.engine_code, 32))
      && (d.final_code === null || text(d.final_code, 32)) && text(d.note, 4000, true)
      && (d.action === "add" ? d.engine_code === null && d.final_code !== null
        : d.engine_code !== null && (d.action === "reject" ? d.final_code === null
          : d.final_code !== null && (d.action === "accept" ? d.final_code === d.engine_code : d.final_code !== d.engine_code)))));
    validateBaselineDecisions(view, receipt.codeDecisions, receipt.baselineDecisions ?? []);
    acceptedDiagnosisOrder(view, receipt.codeDecisions, receipt.humanProposalDecisions);
  }
  if (view.previousSignoff != null) {
    const prior = view.previousSignoff;
    require(!view.signoff && uuid(prior.reviewId) && actorValid(prior.actor)
      && timeValid(prior.createdAt) && Number.isInteger(prior.headerRevision)
      && prior.headerRevision >= 0 && prior.headerRevision < view.revisionNumber);
  }
  return view;
}

export function headerDraft(view) {
  return structuredClone({diagnosisOrderConfirmed: false, ...(view.revision?.draft || {
    facts: Object.fromEntries(view.header.facts.map((f) => [f.key, f.value || ""])),
    codes: view.header.suggestions.map(({id, kind, code, label, evidenceIds}) => ({id, kind, code, label, evidenceIds})),
    note: "",
  })});
}

export function fieldAuthor(view, path) {
  return [...(view.revision?.history || [])].reverse().find((r) => r.changes.some((c) => c.path === path))?.actor;
}

export const headerSourceKey = (encounter) => canonical([encounter.encounterId, encounter.headerSource]);

export function headerProposals(view) {
  const code = ({id, kind, code, label, evidenceIds}) => ({id, kind, code, label, evidenceIds: [...evidenceIds].sort()});
  const original = new Map(view.header.suggestions.map((row) => [row.id, code(row)]));
  const proposed = new Map(headerDraft(view).codes.map((row) => [row.id, code(row)]));
  return [...new Set([...original.keys(), ...proposed.keys()])]
    .filter((id) => !same(original.get(id), proposed.get(id)))
    .map((id) => ({id, original: original.get(id) || null, proposed: proposed.get(id) || null}));
}

export function proposedDiagnosisOrder(view) {
  const draft = headerDraft(view);
  return draft.diagnosisOrderConfirmed === true ? draft.codes.filter((row) => row.kind === "ICD10") : [];
}

export function diagnosisOrderReviewText(view) {
  const receipt = view.signoff;
  const current = receipt?.headerId === view.header.headerId
    && receipt.headerRevision === view.revisionNumber
    && same(receipt.expectedSource, view.header.expectedSource);
  const decision = current ? receipt.humanProposalDecisions
    .find((row) => row.id === diagnosisOrderDecisionId)?.decision : null;
  return decision === "accept" ? "Coder acceptance recorded for this revision."
    : decision === "reject" ? "Coder rejected this proposed primary and order."
      : "Awaiting separate coder acceptance.";
}

export function headerDecisionIds(view) {
  return [...headerProposals(view).map((row) => row.id),
    ...(proposedDiagnosisOrder(view).length ? [diagnosisOrderDecisionId] : [])];
}

// All ICD content is included: changing evidence, membership or ordering clears
// the UI affirmation. Facts and E/M-only edits retain the prior attribution.
export function replaceHeaderCodes(draft, codes) {
  const diagnoses = (rows) => rows.filter((row) => row.kind === "ICD10")
    .map((row) => ({...row, evidenceIds: [...row.evidenceIds].sort()}));
  return {...draft, codes, diagnosisOrderConfirmed: draft.diagnosisOrderConfirmed === true
    && same(diagnoses(draft.codes), diagnoses(codes))};
}

export function moveHeaderDiagnosis(draft, id, target) {
  const indices = draft.codes.flatMap((row, i) => row.kind === "ICD10" ? [i] : []);
  const rows = indices.map((i) => draft.codes[i]);
  const from = rows.findIndex((row) => row.id === id);
  if (from < 0 || !Number.isInteger(target) || target < 0 || target >= rows.length) return draft;
  rows.splice(target, 0, rows.splice(from, 1)[0]);
  const codes = [...draft.codes]; indices.forEach((index, i) => { codes[index] = rows[i]; });
  return replaceHeaderCodes(draft, codes);
}

export function acceptedDiagnosisOrder(view, decisions = view.signoff?.codeDecisions || [],
  choices = view.signoff?.humanProposalDecisions || []) {
  const choice = choices.find((row) => row.id === diagnosisOrderDecisionId);
  if (!choice || choice.decision !== "accept") return [];
  const order = proposedDiagnosisOrder(view);
  const final = new Set(decisions.filter((d) => d.kind === "ICD10"
    && ["accept", "modify", "add"].includes(d.action)).map((d) => d.final_code));
  require(order.length > 0 && final.has(order[0].code)
    && [...final].every((code) => order.some((row) => row.code === code)));
  const retained = order.filter((row) => final.has(row.code));
  const proposals = new Set(headerProposals(view).map((row) => row.id));
  require(retained.every((row) => !proposals.has(row.id)
    || choices.some((c) => c.id === row.id && c.decision === "accept")));
  return retained;
}

export function missingHeaderBaseline(view, decisions) {
  require(list(decisions, 500));
  const current = new Set(decisions.filter((d) => d.action !== "add")
    .map((d) => canonical([d.kind, d.engine_code])));
  return view.header.suggestions.filter((row) => !current.has(canonical([row.kind, row.code])));
}

function validateBaselineDecisions(view, decisions, choices) {
  const missing = new Map(missingHeaderBaseline(view, decisions).map((row) => [row.id, row]));
  const final = new Set(decisions.filter((d) => d.action !== "reject")
    .map((d) => canonical([d.kind, d.final_code])));
  require(list(choices, 20) && choices.length === missing.size);
  for (const choice of choices) {
    const row = missing.get(choice.id);
    require(row && ["retain", "reject"].includes(choice.decision) && text(choice.reason, 1000)
      && final.has(canonical([row.kind, row.code])) === (choice.decision === "retain"));
    missing.delete(choice.id);
  }
}

export function headerReviewRequest(view, feedback, decisions, acknowledged, baselineDecisions = []) {
  require(view && acknowledged === true && !view.signoff
    && feedback.encounter_id === view.header.expectedSource.encounterId
    && feedback.encounter_version === view.header.expectedSource.encounterVersion);
  const proposals = new Set(headerDecisionIds(view));
  require(list(decisions, 41) && decisions.length === proposals.size);
  for (const choice of decisions) require(proposals.delete(choice.id)
    && ["accept", "reject"].includes(choice.decision) && text(choice.reason, 1000));
  validateBaselineDecisions(view, feedback.decisions, baselineDecisions);
  acceptedDiagnosisOrder(view, feedback.decisions, decisions);
  return structuredClone({encounterId: feedback.encounter_id, expectedSource: view.header.expectedSource,
    headerId: view.header.headerId, expectedRevision: view.revisionNumber, feedback,
    acknowledged, humanProposalDecisions: decisions, baselineDecisions});
}

export function exportBillingHeader(view, encounter) {
  // A human sequence must export against the same verified source as the view.
  if (proposedDiagnosisOrder(view).length
    || view.signoff?.humanProposalDecisions?.some((d) => d.id === diagnosisOrderDecisionId)) require(encounter != null);
  if (encounter) readBillingHeader(view, encounter);
  const h = view.header, draft = headerDraft(view), receipt = view.signoff;
  const rows = ["BILLING HEADER", `Encounter: ${h.expectedSource.encounterId}`,
    `Source version: ${h.expectedSource.encounterVersion} | Header revision: ${view.revisionNumber}`,
    `Header ID: ${h.headerId}`, `Generated: ${h.generatedAt}`,
    receipt ? `Coder review recorded: ${receipt.reviewId} by ${receipt.actor.name} at ${receipt.createdAt}`
      : "Coder review pending — unreviewed header",
    "Advisory billing information; clinical note unchanged; no claim submitted.", "", "VISIT FACTS"];
  for (const fact of h.facts) {
    const edited = Boolean(fieldAuthor(view, `facts.${fact.key}`));
    rows.push(`${fact.label}: ${draft.facts[fact.key] || "Unspecified"} (${edited ? "human entry — verify source" : fact.status.replaceAll("_", " ")})${fact.evidenceIds.length ? ` [${fact.evidenceIds.join(", ")}]` : ""}`);
    if (edited) rows.push(`  Original: ${fact.value || "Not documented"}`);
  }
  rows.push("", "ORIGINAL PIPELINE SUGGESTIONS — retain original evidence");
  for (const code of h.suggestions) rows.push(`${code.kind} ${code.code}: ${code.label} [${code.evidenceIds.join(", ")}]`, `  ${code.basis}`);
  if (view.revision) {
    rows.push("", "CURRENT CODE PROPOSALS — human edits are not engine validation");
    for (const code of draft.codes) rows.push(`${code.kind} ${code.code}: ${code.label} [${code.evidenceIds.join(", ")}]`);
    for (const code of h.suggestions.filter((s) => !draft.codes.some((c) => c.id === s.id))) rows.push(`Removal proposed: ${code.kind} ${code.code}`);
    if (draft.note) rows.push(`Reviewer note: ${draft.note}`);
  }
  const proposed = proposedDiagnosisOrder(view), accepted = acceptedDiagnosisOrder(view);
  rows.push("", "HUMAN DIAGNOSIS ORDER");
  if (!proposed.length) rows.push("Unconfirmed — list order does not establish a human primary.");
  else {
    const author = fieldAuthor(view, "diagnosisOrder");
    rows.push(`Saved proposal (revision ${view.revisionNumber})${author ? ` by ${author.name} (${author.role})` : ""}: ${proposed.map((row) => row.code).join(" → ")}`,
      `Proposed primary: ${proposed[0].code}. ${diagnosisOrderReviewText(view)}`);
    for (const row of proposed) rows.push(`  ${row.code} [${row.evidenceIds.join(", ")}]`);
    if (accepted.length) rows.push(`Coder-confirmed primary: ${accepted[0].code}`,
      `Coder-confirmed order: ${accepted.map((row) => row.code).join(" → ")}`);
    else rows.push("Coder-confirmed primary: unresolved. No secondary is substituted.");
  }
  if (receipt) {
    rows.push("", "RECORDED CODER OUTCOMES", `Recommendation digest: ${receipt.recommendationDigest}`);
    for (const d of receipt.codeDecisions) rows.push(`${d.kind} ${d.engine_code || "Human addition"}: ${d.action} → ${d.final_code || "Removed"}${d.note ? ` — ${d.note}` : ""}`);
    for (const d of receipt.humanProposalDecisions) rows.push(`${d.id === diagnosisOrderDecisionId ? "Diagnosis order" : `Header proposal ${d.id}`}: ${d.decision} — ${d.reason}`);
    for (const d of receipt.baselineDecisions || []) {
      const original = h.suggestions.find((row) => row.id === d.id);
      rows.push(`Earlier suggestion ${original.kind} ${original.code}: ${d.decision} — ${d.reason}`);
    }
    if (receipt.reviewerNote) rows.push(receipt.reviewerNote);
  }
  rows.push("", "DOCUMENTATION / POLICY REVIEW", ...h.flags.map((f) =>
    `${f.message}${f.evidenceIds?.length ? ` [${f.evidenceIds.join(", ")}]` : ""}`), h.policy.message,
    "", "SOURCE EVIDENCE — Unicode code-point coordinates");
  for (const e of h.evidence) rows.push(`[${e.id}] ${e.source}, ${e.sourceId}, version ${e.sourceVersion}, ${e.start}–${e.end}`, e.text);
  return rows.join("\n");
}

// Each completion/cleanup may release only the request that owns this ref.
export function ownsHeaderRequest(pending, request) {
  return Boolean(request) && pending.current === request;
}

export function finishHeaderRequest(pending, request) {
  if (!ownsHeaderRequest(pending, request)) return false;
  pending.current = null;
  return true;
}

export function cancelHeaderRequest(pending, request = pending.current) {
  if (finishHeaderRequest(pending, request)) request.controller.abort();
}

export function beginHeaderRequest(pending, command = null, refresh = false, unresolvedCommand = null) {
  // An ambiguous save keeps its exact command even after its network request ends.
  if (unresolvedCommand && command !== unresolvedCommand) return null;
  // Refresh may replace reads, but must not discard an unresolved mutation.
  if (refresh && pending.current && !pending.current.command) cancelHeaderRequest(pending);
  if (pending.current) return null;
  pending.current = {controller: new AbortController(), command};
  return pending.current;
}
