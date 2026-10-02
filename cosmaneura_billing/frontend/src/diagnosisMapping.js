const canonical = (v) => JSON.stringify(v, (_, value) => value && !Array.isArray(value) && typeof value === "object"
  ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b))) : value);
const same = (a, b) => canonical(a) === canonical(b);
const version = (v) => typeof v === "string" && /^icd10cm-sha256:[0-9a-f]{64}$/.test(v);
export const mappingCandidates = (header) => header.flags.filter((flag) =>
  /^diagnosis-review-[0-9]+$/.test(flag.id) && flag.evidenceIds?.length);

export function validMapping(row, header) {
  const m = row.mapping;
  if (!m || Object.keys(m).sort().join() !== "candidateId,catalogVersion" || !version(m.catalogVersion)
    || row.kind !== "ICD10" || header.suggestions.some((s) => s.id === row.id)) return false;
  const candidate = mappingCandidates(header).find((c) => c.id === m.candidateId);
  return !!candidate && same([...row.evidenceIds].sort(), [...candidate.evidenceIds].sort());
}

export function readCatalog(response, view, candidate) {
  const source = response?.expectedSource, expected = view.header.expectedSource;
  if (response?.headerId !== view.header.headerId || response.revision !== view.revisionNumber
    || !same(source, expected)
    || response.candidateId !== candidate.id || !version(response.catalogVersion)
    || !Array.isArray(response.matches) || response.matches.length > 20
    || !(response.nextOffset === null || Number.isInteger(response.nextOffset) && response.nextOffset >= 20 && response.nextOffset <= 1_000_000)
    || response.matches.some((row) => !/^[A-Z][0-9][A-Z0-9](?:\.[A-Z0-9]{1,4})?$/.test(row.code)
      || typeof row.description !== "string" || !row.description.trim() || row.description.length > 1000
      || row.label !== row.description.slice(0, 150))
    || new Set(response.matches.map((r) => r.code)).size !== response.matches.length) throw new Error("invalid-catalog-response");
  return response;
}

export function mappingProposal(candidate, match, catalogVersion, id) {
  return {id, kind: "ICD10", code: match.code, label: match.label,
    evidenceIds: [...candidate.evidenceIds], mapping: {candidateId: candidate.id, catalogVersion}};
}

export function mappingReviewText(view, row) {
  const decision = view.signoff?.humanProposalDecisions.find((d) => d.id === row.id);
  return decision?.decision === "accept" ? "Coder accepted this human mapping."
    : decision?.decision === "reject" ? "Coder rejected this human mapping."
      : "Human mapping proposed; coder review pending.";
}

export function validMappingOutcomes(view, decisions, choices) {
  const final = new Set(decisions.filter((d) => d.kind === "ICD10" && ["accept", "modify", "add"].includes(d.action)).map((d) => d.final_code));
  return (view.revision?.draft.codes || []).filter((row) => row.mapping).every((row) => {
    const choice = choices.find((d) => d.id === row.id);
    return choice?.decision === "accept" ? final.has(row.code) : choice?.decision === "reject" && !final.has(row.code);
  });
}

export function mappingAddition(view, row, id) {
  if (!validMapping(row, view.header)) throw new Error("invalid-mapping");
  const evidence = view.header.evidence.find((e) => row.evidenceIds.includes(e.id));
  if (!evidence || evidence.text.length > 1900) throw new Error("mapping-evidence-needs-manual-review");
  return {id, sourceKey: `header:${view.header.headerId}:${view.revisionNumber}:${row.id}`,
    kind: "ICD10", code: row.code, rationale: "", evidence: evidence.text};
}
