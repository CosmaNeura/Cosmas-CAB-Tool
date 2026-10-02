import React, {useEffect, useRef, useState} from "react";
import {api} from "./api.js";
import {mappingCandidates, mappingProposal, readCatalog} from "./diagnosisMapping.js";

const button = "rounded-lg border border-slate-300 px-3 py-2 text-xs font-medium disabled:opacity-50";
export default function DiagnosisMapping({view, draft, disabled, onChoose}) {
  // Definitive save conflicts clear the source-bound view while retaining inputs.
  if (!view?.header) return null;
  const candidates = mappingCandidates(view.header);
  if (!candidates.length) return null;
  return <fieldset disabled={disabled} className="mt-4 space-y-3"><legend className="text-sm font-semibold">Resolve a held diagnosis</legend>
    <p className="text-xs text-slate-600">Review the original evidence, then search the local ICD-10-CM catalog. Catalog matches are terminology options, not engine suggestions. Selection creates an unreviewed human proposal.</p>
    {candidates.map((candidate) => <MappingSearch key={candidate.id} {...{candidate, view, draft, disabled, onChoose}} />)}
  </fieldset>;
}

function MappingSearch({candidate, view, draft, disabled, onChoose}) {
  const [query, setQuery] = useState(""), [result, setResult] = useState(null);
  const [error, setError] = useState(""), [busy, setBusy] = useState(false);
  const sequence = useRef(0), controller = useRef(null);
  useEffect(() => () => {sequence.current++; controller.current?.abort();}, []);
  function changeQuery(value) {sequence.current++; controller.current?.abort(); setBusy(false); setQuery(value); setResult(null); setError("");}
  async function search(offset = 0) {
    const seq = ++sequence.current;
    controller.current?.abort(); controller.current = new AbortController(); setBusy(true); setResult(null); setError("");
    try {
      const response = await api("/api/encounters/header/diagnosis-catalog", {method: "POST", signal: controller.current.signal,
        body: {encounterId: view.header.expectedSource.encounterId, expectedSource: view.header.expectedSource,
          headerId: view.header.headerId, expectedRevision: view.revisionNumber, candidateId: candidate.id, query, offset}});
      if (seq === sequence.current) setResult(readCatalog(response, view, candidate));
    } catch (failure) {
      if (seq === sequence.current && failure.name !== "AbortError") setError(failure.status === 409
        ? "The header or source changed. Reload before selecting a mapping."
        : "Catalog lookup unavailable. No replacement mapping was selected.");
    } finally {if (seq === sequence.current) setBusy(false);}
  }
  return <div className="rounded-lg border border-amber-200 bg-amber-50 p-3" role="group" aria-label={`Map ${candidate.id}`}>
    <p className="text-sm font-medium">{candidate.message}</p>
    {candidate.evidenceIds.map((id) => <blockquote key={id} className="mt-2 border-l-2 border-amber-300 pl-3 text-xs">{view.header.evidence.find((e) => e.id === id).text}</blockquote>)}
    <label className="mt-3 block text-xs font-medium">Search ICD catalog for {candidate.id}
      <input className="mt-1 w-full rounded border border-slate-300 bg-white p-2" maxLength={80} value={query} onChange={(e) => changeQuery(e.target.value)} /></label>
    <button type="button" className={`${button} mt-2 bg-white`} disabled={disabled || busy || query.trim().length < 2} onClick={() => search()}>Search catalog</button>
    {busy && <p role="status" className="mt-2 text-xs">Searching local catalog…</p>}
    {error && <p role="alert" className="mt-2 text-xs">{error}</p>}
    {result && <div className="mt-2"><p className="text-xs">{result.matches.length ? "Choose only after verifying the evidence and code-defining details." : "No catalog matches. Refine the code or description."}</p>
      <ul className="mt-2 space-y-2">{result.matches.map((match) => {
        const existing = draft.codes.find((row) => row.mapping?.candidateId === candidate.id);
        const duplicate = draft.codes.some((row) => row.kind === "ICD10" && row.code === match.code && row.id !== existing?.id);
        return <li key={match.code} className="rounded border border-slate-200 bg-white p-2 text-xs"><strong>{match.code}</strong> · {match.description}
          <button type="button" className={`${button} ml-2`} disabled={disabled || duplicate || (!existing && draft.codes.length >= 20)} onClick={() => {
            onChoose(mappingProposal(candidate, match, result.catalogVersion, existing?.id || `human-${crypto.randomUUID()}`));
            setResult(null);
          }}>{duplicate ? "Already proposed" : `Propose ${match.code}`}</button></li>;
      })}</ul>
      {result.nextOffset !== null && <button type="button" className={`${button} mt-2`} onClick={() => search(result.nextOffset)}>Next catalog page</button>}
    </div>}
  </div>;
}
