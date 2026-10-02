// Validate the whole header against the currently displayed source before render.
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
  require(list(h.flags, 30) && h.flags.every((f) => id(f.id) && text(f.message, 700))
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
    require(draft && same(Object.keys(draft.facts).sort(), [...factKeys].sort())
      && Object.values(draft.facts).every((v) => text(v, 600, true))
      && list(draft.codes, 20) && draft.codes.every(codeValid) && text(draft.note, 2000, true));
    require(list(revision.history, 100) && revision.history.length === revision.number);
    for (const item of revision.history) {
      require(actorValid(item.actor) && timeValid(item.createdAt) && text(item.reason, 600)
        && list(item.changes, 50) && item.changes.every((c) => text(c.path, 180)
          && text(c.label, 150) && JSON.stringify(c.before)?.length <= 10000 && JSON.stringify(c.after)?.length <= 10000));
    }
  }
  return view;
}

export function headerDraft(view) {
  return structuredClone(view.revision?.draft || {
    facts: Object.fromEntries(view.header.facts.map((f) => [f.key, f.value || ""])),
    codes: view.header.suggestions.map(({id, kind, code, label, evidenceIds}) => ({id, kind, code, label, evidenceIds})),
    note: "",
  });
}

export function fieldAuthor(view, path) {
  return [...(view.revision?.history || [])].reverse().find((r) => r.changes.some((c) => c.path === path))?.actor;
}
