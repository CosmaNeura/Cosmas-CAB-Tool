import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, Ban, ChevronDown, ClipboardCheck, Copy, Database,
  FileText, RefreshCw, ShieldCheck, Stethoscope, X,
} from "lucide-react";

import { api } from "./api.js";
import ProviderQueue from "./ProviderQueue.jsx";
import BillingHeader from "./BillingHeader.jsx";
import { readWorkspaceEncounter, readWorkspaceResult, workspaceInferenceAllowed } from "./workspaceSource.js";
import MdmDataReview from "./MdmDataReview.jsx";
import { dataSectionSummary, readDataReview } from "./mdmDataReview.js";
import MdmClinicalReview from "./MdmClinicalReview.jsx";
import { clinicalSectionSummary, readClinicalReview } from "./mdmClinicalReview.js";
import DiagnosisSequencingNotice from "./DiagnosisSequencingNotice.jsx";
import { primaryBadge } from "./diagnosisSequencing.js";
import {
  completenessLabel, evidenceSupportedEm,
  isCurrentEncounterResult, isLocatedEvidenceSpan,
  preparedBillingBlock, sourceLabel,
  suggestedCandidates, visitTypeLabel,
} from "./providerBilling.js";

const TONES = Object.freeze({
  suggested: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  held: "bg-amber-50 text-amber-800 ring-amber-200",
  removed: "bg-rose-50 text-rose-800 ring-rose-200",
  present: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  missing: "bg-rose-50 text-rose-800 ring-rose-200",
  ambiguous: "bg-amber-50 text-amber-800 ring-amber-200",
});

function Pill({ tone = "bg-slate-100 text-slate-700 ring-slate-200", children }) {
  return <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>{children}</span>;
}

function ErrorNotice({ children }) {
  return <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">{children}</div>;
}

function Evidence({ span, label = "Evidence" }) {
  const location = `characters ${span.loc.start}–${span.loc.end}`;
  return (
    <details className="group rounded-lg border border-slate-200 bg-slate-50">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs font-medium text-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700">
        <ChevronDown size={14} className="transition group-open:rotate-180" aria-hidden="true" />
        {label} from {String(span.source || "documentation").replaceAll("_", " ")} · {location}
      </summary>
      <blockquote className="border-t border-slate-200 px-3 py-2 text-sm italic leading-relaxed text-slate-700">“{span.text}”</blockquote>
    </details>
  );
}

function DocumentDisclosure({ title, text }) {
  return (
    <details className="group rounded-xl border border-slate-200 bg-white">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 text-sm font-medium text-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700">
        <span className="inline-flex items-center gap-2"><FileText size={15} aria-hidden="true" />{title}</span>
        <ChevronDown size={15} className="transition group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="max-h-72 overflow-auto whitespace-pre-wrap border-t border-slate-200 bg-slate-50 px-4 py-3 text-sm leading-relaxed text-slate-700">
        {text || "No text supplied."}
      </div>
    </details>
  );
}

function EmReview({ em, supportedEm, encounter, dataReview, clinicalReview }) {
  if (!em) return <section aria-label="E/M review" className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-semibold">E/M review</h2><p className="mt-2 text-sm text-slate-600">No evidence-supported E/M level was produced.</p><MdmDataReview review={dataReview} encounter={encounter} /><MdmClinicalReview review={clinicalReview} encounter={encounter} /></section>;
  let clinical, data;
  try { clinical = readClinicalReview(clinicalReview, encounter); } catch { clinical = null; }
  try { data = readDataReview(dataReview, encounter); } catch { data = null; }
  const verifiedEvidence = (em.evidence_spans || []).filter(
    (span) => isLocatedEvidenceSpan(span, encounter),
  );
  const hasVerifiedEvidence = verifiedEvidence.length > 0;
  const reviewedLabel = supportedEm
    ? "Evidence-supported"
    : hasVerifiedEvidence ? "Held for review" : "Withheld — no verified evidence";
  const reviewedCode = hasVerifiedEvidence ? (supportedEm?.code || em.suggested_level) : null;
  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="em-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 id="em-heading" className="text-base font-semibold text-slate-950">E/M review</h2><p className="mt-1 text-sm text-slate-600">Compare the provider-selected level with documented support.</p></div>
        <Pill tone={TONES[em.status]}>{em.status}</Pill>
      </div>
      <dl className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className="rounded-xl bg-slate-50 p-3"><dt className="text-xs font-medium uppercase tracking-wide text-slate-500">Provider selected</dt><dd className="mt-1 font-mono text-lg font-semibold">{em.provider_selected_level || "None"}</dd></div>
        <div className={`rounded-xl p-3 ring-1 ${supportedEm ? "bg-sky-50 ring-sky-100" : "bg-amber-50 ring-amber-100"}`}><dt className={`text-xs font-medium uppercase tracking-wide ${supportedEm ? "text-sky-700" : "text-amber-700"}`}>{reviewedLabel}</dt><dd className={`mt-1 font-mono text-lg font-semibold ${supportedEm ? "text-sky-950" : "text-amber-950"}`}>{reviewedCode || "No code displayed"}</dd></div>
      </dl>
      <p className="mt-3 text-sm text-slate-700">Pathway: <strong>{String(em.pathway || "not reported").replaceAll("_", " ")}</strong>{em.mdm_code ? ` · MDM ${em.mdm_code}` : ""}{em.total_time_minutes != null ? ` · ${em.total_time_minutes} documented minutes` : ""}</p>
      {em.status_reason && <p className="mt-2 text-sm text-amber-800">{em.status_reason}</p>}
      {em.why && <p className="mt-2 text-sm text-slate-700"><strong>Why:</strong> {em.why}</p>}
      <h3 className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-500">Medical decision-making facts</h3>
      <div className="mt-2 grid gap-3 md:grid-cols-3">
        {(em.mdm_facts || []).map((fact) => (
          <div key={fact.element} className="rounded-xl border border-slate-200 p-3">
            <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{fact.element}</p>
            <p className="mt-1 text-sm font-semibold text-slate-900">{fact.element === "data" && dataReview !== undefined
              ? data ? dataSectionSummary(data) : "Data evidence not verified"
              : clinicalReview !== undefined && ["problems", "risk"].includes(fact.element)
                ? clinical ? clinicalSectionSummary(clinical[fact.element]) : "Clinical evidence not verified"
                : fact.finding || "Not established"}</p>
            <div className="mt-2">{fact.element === "data" && em.data_review ? <p className="text-xs text-slate-500">See the data-review ledger below for counted, duplicate, and held items.</p>
              : clinicalReview !== undefined && ["problems", "risk"].includes(fact.element) ? <p className="text-xs text-slate-500">See the clinical-review ledger below for the original decisions and source evidence.</p>
                : isLocatedEvidenceSpan(fact.evidence_span, encounter) ? <Evidence span={fact.evidence_span} /> : <p className="text-xs text-slate-500">No exactly located element-specific evidence was reported.</p>}</div>
          </div>
        ))}
      </div>
      <MdmDataReview review={dataReview} encounter={encounter} />
      <MdmClinicalReview review={clinicalReview} encounter={encounter} />
      {verifiedEvidence.length > 0 && (
        <details className="group mt-4 rounded-xl border border-slate-200 bg-slate-50">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700"><ChevronDown size={15} className="transition group-open:rotate-180" aria-hidden="true" />All E/M evidence ({verifiedEvidence.length})</summary>
          <div className="space-y-2 border-t border-slate-200 p-3">{verifiedEvidence.map((span, index) => <Evidence key={`${span.source}-${span.loc.start}-${index}`} span={span} />)}</div>
        </details>
      )}
      {(em.guardrail_actions || []).length > 0 && (
        <details className="group mt-4 rounded-xl border border-slate-200 bg-slate-50">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700"><ChevronDown size={15} className="transition group-open:rotate-180" aria-hidden="true" />Compliance guardrail actions</summary>
          <ul className="space-y-1 border-t border-slate-200 px-5 py-3 text-sm text-slate-700">{em.guardrail_actions.map((action) => <li key={action}>{action}</li>)}</ul>
        </details>
      )}
    </section>
  );
}

function CodeCandidate({ candidate, primary, onCopy }) {
  const evidence = candidate.evidence_spans || [];
  return (
    <article className="rounded-xl border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-base font-semibold text-slate-950">{candidate.code}</code>
            <Pill tone={TONES[candidate.status]}>{candidate.status}</Pill>
            {primary && <Pill tone="bg-sky-50 text-sky-800 ring-sky-200">{primary}</Pill>}
          </div>
          <p className="mt-1 text-sm text-slate-700">{candidate.description}</p>
        </div>
        {candidate.status === "suggested" && (
          <button type="button" onClick={() => onCopy(candidate.code, `Code ${candidate.code} copied — not sent.`)} aria-label={`Copy suggested code ${candidate.code}`} className="inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700">
            <Copy size={15} aria-hidden="true" />Copy
          </button>
        )}
      </div>
      {candidate.status !== "suggested" && (
        <div className={`mt-3 flex gap-2 rounded-lg p-3 text-sm ${candidate.status === "removed" ? "bg-rose-50 text-rose-900" : "bg-amber-50 text-amber-900"}`}>
          {candidate.status === "removed" ? <X size={16} className="mt-0.5 shrink-0" aria-hidden="true" /> : <AlertTriangle size={16} className="mt-0.5 shrink-0" aria-hidden="true" />}
          <span><strong>{candidate.status === "removed" ? "Removed:" : "Held:"}</strong> {candidate.status_reason || candidate.why}</span>
        </div>
      )}
      {candidate.status === "suggested" && candidate.why && <p className="mt-3 text-sm text-slate-700"><strong>Why:</strong> {candidate.why}</p>}
      {candidate.denial_risk && <p className="mt-2 text-xs text-slate-500">Denial-risk classification: <strong>{candidate.denial_risk}</strong></p>}
      {candidate.coverage_note && <p className="mt-2 text-xs text-slate-500">Coverage context: {candidate.coverage_note}</p>}
      {(candidate.alternates || []).length > 0 && <p className="mt-2 text-sm text-slate-700">Alternate mappings: <span className="font-mono">{candidate.alternates.join(", ")}</span></p>}
      {(candidate.linked_plan_items || []).length > 0 && (
        <details className="group mt-3 rounded-lg border border-slate-200 bg-slate-50">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700"><ChevronDown size={14} className="transition group-open:rotate-180" aria-hidden="true" />Linked assessment / plan items ({candidate.linked_plan_items.length})</summary>
          <ul className="list-disc space-y-1 border-t border-slate-200 px-7 py-2 text-sm text-slate-700">{candidate.linked_plan_items.map((item) => <li key={item}>{item}</li>)}</ul>
        </details>
      )}
      {evidence.length > 0 && <div className="mt-3 space-y-2">{evidence.map((span, index) => <Evidence key={`${span.source}-${span.loc.start}-${index}`} span={span} label={candidate.status === "suggested" ? "Evidence" : "Decision evidence"} />)}</div>}
    </article>
  );
}

function CandidateGroup({ title, candidates, primaryCode, primaryLabel, onCopy }) {
  if (!candidates?.length) return null;
  const headingId = `${title.replaceAll(" ", "-").toLowerCase()}-heading`;
  return (
    <section aria-labelledby={headingId}>
      <h2 id={headingId} className="text-base font-semibold text-slate-950">{title}</h2>
      <p className="mb-3 text-xs text-slate-500">Server ordered; coverage context may rank legitimate codes but never creates one.</p>
      <div className="space-y-3">{candidates.map((candidate, index) => <CodeCandidate key={`${candidate.code}-${candidate.status}-${index}`} candidate={candidate} primary={candidate.status === "suggested" && candidate.code === primaryCode ? primaryLabel : null} onCopy={onCopy} />)}</div>
    </section>
  );
}

function CompletenessReview({ report, loading, error }) {
  if (loading) return <p role="status" className="text-sm text-slate-600">Checking documentation completeness…</p>;
  if (error) return <ErrorNotice>{error}</ErrorNotice>;
  if (!report) return <p className="text-sm text-slate-600">Completeness has not been checked.</p>;
  if (report.rulepack == null) return <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-700">No deterministic completeness rule pack covers this visit type yet. This is not a passing result.</div>;
  const attestation = report.attestation_block ?? report.attestationBlock ?? null;
  const overrideCount = (report.results || []).filter((item) => item.provider_override?.overridden === true).length;
  const clearedWithOverride = report.complete === true && overrideCount > 0;
  return (
    <div>
      <div className={`mb-4 rounded-xl border p-4 ${clearedWithOverride ? "border-violet-200 bg-violet-50 text-violet-900" : report.complete ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-rose-200 bg-rose-50 text-rose-900"}`}>
        <p className="font-semibold">{clearedWithOverride ? "Completeness cleared with reasoned override" : report.complete ? "All completeness requirements were found" : "Documentation completeness blocks a ready state"}</p>
        <p className="mt-1 text-sm">Rule pack <code className="font-mono">{report.rulepack}</code> · {completenessLabel(report)}</p>
      </div>
      <div className="space-y-3">
        {(report.results || []).map((item) => {
          const overridden = item.provider_override?.overridden === true;
          return (
            <article key={item.rule_id} className="rounded-xl border border-slate-200 bg-white p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div><p className="font-mono text-xs text-slate-500">{item.rule_id}</p><p className="mt-1 text-sm font-medium text-slate-900">Document {item.asks_for}.</p></div>
                <Pill tone={overridden ? "bg-violet-50 text-violet-800 ring-violet-200" : TONES[item.status]}>{overridden ? "overridden with reason" : item.status}</Pill>
              </div>
              {item.evidence && <p className="mt-2 text-sm italic text-slate-700">“{item.evidence}”</p>}
              {overridden ? <p className="mt-2 rounded-lg bg-violet-50 p-3 text-sm text-violet-900"><strong>Override reason:</strong> {item.provider_override.reason}</p> : item.status !== "present" ? <p className="mt-2 text-sm text-slate-600">{item.fix_hint}</p> : null}
            </article>
          );
        })}
      </div>
      {report.complete && attestation && (
        <details className="group mt-4 rounded-xl border border-emerald-200 bg-emerald-50">
          <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-semibold text-emerald-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-700"><ChevronDown size={15} className="transition group-open:rotate-180" aria-hidden="true" />View completeness attestation</summary>
          <pre className="overflow-auto whitespace-pre-wrap border-t border-emerald-200 px-4 py-3 text-xs leading-relaxed text-emerald-950">{attestation}</pre>
        </details>
      )}
    </div>
  );
}

function safeCandidates(candidates = [], encounter) {
  return candidates.flatMap((candidate) => {
    const evidence = Array.isArray(candidate.evidence_spans)
      ? candidate.evidence_spans.filter(
        (span) => isLocatedEvidenceSpan(span, encounter),
      ) : [];
    return evidence.length > 0 ? [{ ...candidate, evidence_spans: evidence }] : [];
  });
}

function billingErrorMessage(error) {
  if (error?.status === 401) return "Your session expired. Sign in again before reviewing this encounter.";
  if (error?.status === 403) return "Your assigned role cannot review this encounter.";
  if (error?.status === 404) return "This encounter is no longer available. Refresh the queue.";
  if (error?.message === "encounter-version-response") return "The encounter changed after it was loaded. Refresh and review the current documentation before generating billing guidance.";
  return "The billing review could not be generated. No browser fallback or canned result was substituted.";
}

export default function BillingReview({ encounters, selectedId, live, statuses, onSelect, onStatusChange,
  onHeaderNavigationChange, navigationLocked = false, headerAsync = false,
  savedProgress, onProgressChange, onRefreshProgress, refreshingProgress, progressRefreshDisabled }) {
  const [encounter, setEncounter] = useState(null);
  const [result, setResult] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [reviewError, setReviewError] = useState(null);
  const [running, setRunning] = useState(false);
  const [completeness, setCompleteness] = useState(null);
  const [completenessLoading, setCompletenessLoading] = useState(false);
  const [completenessError, setCompletenessError] = useState(null);
  const [copyStatus, setCopyStatus] = useState("");
  const detailRequest = useRef(0);
  const reviewRequest = useRef(0);
  const reviewAbort = useRef(null);
  const copyRequest = useRef(0);
  const activeDetailEncounter = useRef(null);
  const activeReviewEncounter = useRef(null);

  useEffect(() => {
    const requestId = ++detailRequest.current;
    ++reviewRequest.current;
    ++copyRequest.current;
    reviewAbort.current?.abort();
    const controller = new AbortController();
    setEncounter(null); setResult(null); setLoadError(null); setReviewError(null);
    setCompleteness(null); setCompletenessError(null); setCopyStatus("");
    setCompletenessLoading(false); setRunning(false);
    const cancelRequests = () => {
      controller.abort();
      reviewAbort.current?.abort();
      if (activeDetailEncounter.current === selectedId) {
        onStatusChange(selectedId, { processingStatus: "Load canceled" });
        activeDetailEncounter.current = null;
      }
      const canceledReview = activeReviewEncounter.current;
      if (canceledReview) {
        onStatusChange(canceledReview, { processingStatus: "Review canceled" });
        activeReviewEncounter.current = null;
      }
    };
    if (!selectedId) return cancelRequests;
    if (!live) {
      setLoadError("The billing service is unavailable. No browser fallback or canned result was loaded.");
      onStatusChange(selectedId, { processingStatus: "Service unavailable" });
      return () => { controller.abort(); reviewAbort.current?.abort(); };
    }
    activeDetailEncounter.current = selectedId;
    onStatusChange(selectedId, { processingStatus: "Loading" });
    (async () => {
      try {
        const detail = await api("/api/encounters/detail", { method: "POST", body: { encounterId: selectedId }, signal: controller.signal });
        if (requestId !== detailRequest.current) return;
        if (detail.id !== selectedId) {
          setLoadError("The server returned a different encounter than the one selected. Nothing was loaded.");
          onStatusChange(selectedId, { processingStatus: "Encounter mismatch" });
          activeDetailEncounter.current = null;
          return;
        }
        const listed = encounters.find((item) => item.id === selectedId);
        const loaded = await readWorkspaceEncounter(detail, selectedId, listed);
        if (requestId !== detailRequest.current) return;
        setCompletenessLoading(true);
        try {
          const report = await api("/api/completeness", {
            method: "POST", signal: controller.signal,
            body: { note: loaded.signedNote, placeOfService: loaded.placeOfService, encounterType: loaded.encounterType },
          });
          if (requestId !== detailRequest.current) return;
          setCompleteness(report);
          setEncounter(loaded);
          onStatusChange(selectedId, { completenessState: completenessLabel(report) });
        } catch (error) {
          if (error?.name === "AbortError" || requestId !== detailRequest.current) return;
          setEncounter(loaded);
          setCompletenessError("Completeness could not be checked. This encounter cannot be presented as ready.");
          onStatusChange(selectedId, { completenessState: "Check failed" });
        } finally {
          if (requestId === detailRequest.current) {
            setCompletenessLoading(false);
            onStatusChange(selectedId, { processingStatus: "Ready to review" });
            activeDetailEncounter.current = null;
          }
        }
      } catch (error) {
        if (error?.name === "AbortError" || requestId !== detailRequest.current) return;
        setLoadError(billingErrorMessage(error));
        onStatusChange(selectedId, { processingStatus: "Load failed" });
        activeDetailEncounter.current = null;
      }
    })();
    return cancelRequests;
  }, [selectedId, live, onStatusChange, encounters]);

  const runReview = async () => {
    if (!encounter || !live || !workspaceInferenceAllowed(encounter)) return;
    const requestedEncounterId = encounter.encounterId;
    const requestId = ++reviewRequest.current;
    ++copyRequest.current;
    reviewAbort.current?.abort();
    const controller = new AbortController();
    reviewAbort.current = controller;
    activeReviewEncounter.current = requestedEncounterId;
    setRunning(true); setReviewError(null); setResult(null); setCopyStatus("");
    onStatusChange(requestedEncounterId, { processingStatus: "Reviewing" });
    try {
      const review = await api("/api/encounters/billing-tab", { method: "POST", body: { encounterId: requestedEncounterId }, signal: controller.signal });
      if (requestId !== reviewRequest.current || selectedId !== requestedEncounterId) return;
      if (review.visit_id !== requestedEncounterId) throw new Error("encounter-mismatch-response");
      readWorkspaceResult(review, encounter, "visit_id");
      if (!isCurrentEncounterResult(review, encounter)) {
        throw new Error("encounter-version-response");
      }
      if (review.clinical_note_modified !== false) throw new Error("note-integrity-response");
      setResult(review);
      if (review.completeness) {
        setCompleteness(review.completeness);
        setCompletenessError(null);
        setCompletenessLoading(false);
      }
      onStatusChange(requestedEncounterId, {
        ...(review.degraded
          ? { reviewState: "Manual review", processingStatus: "Inference unavailable" }
          : { reviewState: "Provider review", processingStatus: "Review generated" }),
        ...(review.completeness
          ? { completenessState: completenessLabel(review.completeness) }
          : {}),
      });
    } catch (error) {
      if (error?.name === "AbortError" || requestId !== reviewRequest.current) return;
      setReviewError(billingErrorMessage(error));
      onStatusChange(requestedEncounterId, { processingStatus: "Review failed" });
    } finally {
      if (requestId === reviewRequest.current) {
        setRunning(false);
        activeReviewEncounter.current = null;
      }
    }
  };

  const copyText = async (text, confirmation) => {
    const requestId = ++copyRequest.current;
    if (!text || !navigator.clipboard?.writeText) { setCopyStatus("Copy is unavailable in this browser. Nothing was sent."); return; }
    try {
      await navigator.clipboard.writeText(text);
      if (requestId === copyRequest.current) setCopyStatus(confirmation);
    } catch (_error) {
      if (requestId === copyRequest.current) setCopyStatus("Copy permission was denied. Nothing was sent.");
    }
  };

  const icdCandidates = safeCandidates(result?.icd_candidates, encounter);
  const cptCandidates = safeCandidates(result?.cpt_candidates, encounter);
  const primaryDiagnosis = icdCandidates.find((candidate) => candidate.primary === true)?.code || null;
  const primaryLabel = primaryBadge(result?.diagnosis_sequencing);
  const supportedEm = evidenceSupportedEm(result, encounter);
  const preparedBlock = useMemo(
    () => preparedBillingBlock(result ? { ...result, completeness } : result, encounter),
    [result, completeness, encounter],
  );
  const suggested = suggestedCandidates(result, encounter);
  const derivedCopyAll = suggested.map((candidate) => candidate.code).join(" ");
  const safeCopyAll = result?.copy_all === derivedCopyAll ? result.copy_all : derivedCopyAll;
  const completenessReady = completeness?.complete === true;
  const completenessBlocked = completeness?.complete === false;
  const completenessOverridden = completenessReady && (completeness?.results || []).some(
    (item) => item.provider_override?.overridden === true,
  );

  return (
    <div className="space-y-5">
      <ProviderQueue encounters={encounters} selectedId={selectedId} statuses={statuses} onSelect={onSelect} selectionDisabled={navigationLocked}
        savedProgress={savedProgress} onRefreshProgress={onRefreshProgress} refreshingProgress={refreshingProgress} progressRefreshDisabled={progressRefreshDisabled} />
      {!selectedId ? (
        <section className="rounded-2xl border border-dashed border-slate-300 bg-white p-7 text-center">
          <ClipboardCheck size={26} className="mx-auto text-slate-400" aria-hidden="true" />
          <h2 className="mt-2 font-semibold text-slate-950">Select an encounter to begin</h2>
          <p className="mt-1 text-sm text-slate-600">Start with documentation completeness, then run the evidence-linked billing review.</p>
        </section>
      ) : loadError ? <ErrorNotice>{loadError}</ErrorNotice> : !encounter || encounter.encounterId !== selectedId ? (
        <p role="status" className="text-sm text-slate-600">Loading encounter documentation…</p>
      ) : (
        <>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="provider-review-heading">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="flex items-center gap-2"><span className="flex h-9 w-9 items-center justify-center rounded-lg bg-sky-50 text-sky-800" aria-hidden="true"><Stethoscope size={18} /></span><div><h1 id="provider-review-heading" className="text-lg font-semibold text-slate-950">Provider Billing Review</h1><p className="text-sm text-slate-600">Review documented support, then copy a prepared reference for a human-controlled handoff.</p></div></div>
              <div className="text-right text-xs text-slate-600"><p className="font-mono font-semibold text-slate-900">{encounter.encounterId}</p><p>{visitTypeLabel(encounter.encounterType)}</p></div>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <Pill tone="bg-violet-50 text-violet-800 ring-violet-200"><Database size={12} className="mr-1" aria-hidden="true" />{sourceLabel(encounter.dataClass)}</Pill>
              {encounter.readOnly && <Pill><ShieldCheck size={12} className="mr-1" aria-hidden="true" />Read-only documentation</Pill>}
              <Pill>Documentation version {encounter.encounterVersion}</Pill>
              <Pill>Submitted E/M: {encounter.submittedEm || "none"}</Pill>
            </div>
            <div className="mt-4 grid gap-2 md:grid-cols-3"><DocumentDisclosure title="Intake" text={encounter.intake} /><DocumentDisclosure title="Visit transcript" text={encounter.transcript} /><DocumentDisclosure title="Clinical note" text={encounter.signedNote} /></div>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button type="button" onClick={runReview} disabled={running || !workspaceInferenceAllowed(encounter)} className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-slate-950 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-wait disabled:opacity-60">
                {running ? <RefreshCw size={16} className="animate-spin" aria-hidden="true" /> : <ClipboardCheck size={16} aria-hidden="true" />}{running ? "Generating review…" : result ? "Regenerate billing review" : "Run billing review"}
              </button>
              <p className="text-xs text-slate-500">Uses server-stored documentation. CAB does not edit the original clinical note.</p>
            </div>
            {reviewError && <div className="mt-4"><ErrorNotice>{reviewError}</ErrorNotice></div>}
          </section>

          {encounter.headerSource && <BillingHeader asyncEnabled={headerAsync} key={`${encounter.encounterId}:${encounter.headerSource.bundleSha256}:${encounter.encounterVersion}`} encounter={encounter} generationAllowed={workspaceInferenceAllowed(encounter)} onStateChange={onHeaderNavigationChange} onProgressChange={onProgressChange} />}

          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="completeness-heading">
            <h2 id="completeness-heading" className="text-base font-semibold text-slate-950">Documentation completeness</h2>
            <p className="mb-4 mt-1 text-sm text-slate-600">Deterministic rules only. Missing or ambiguous requirements block a ready state. Overrides are displayed only when returned with a reason; this view does not create or persist them.</p>
            <CompletenessReview report={completeness} loading={completenessLoading} error={completenessError} />
          </section>

          {result?.degraded ? (
            <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-amber-950 shadow-sm" aria-labelledby="degraded-heading"><div className="flex gap-3"><AlertTriangle size={20} className="mt-0.5 shrink-0" aria-hidden="true" /><div><h2 id="degraded-heading" className="font-semibold">Inference is unavailable for this encounter</h2><p className="mt-1 text-sm">{result.degraded.detail}</p><p className="mt-2 text-sm">No code recommendations, copy actions, or prepared block are shown. Route this encounter to manual review.</p></div></div></section>
          ) : result ? (
            <>
              <div className={`rounded-xl border p-4 ${completenessBlocked ? "border-rose-200 bg-rose-50 text-rose-900" : completenessOverridden ? "border-violet-200 bg-violet-50 text-violet-950" : completenessReady ? "border-sky-200 bg-sky-50 text-sky-950" : "border-amber-200 bg-amber-50 text-amber-950"}`}><div className="flex gap-3">{completenessBlocked ? <Ban size={18} className="mt-0.5 shrink-0" aria-hidden="true" /> : completenessReady ? <ShieldCheck size={18} className="mt-0.5 shrink-0" aria-hidden="true" /> : <AlertTriangle size={18} className="mt-0.5 shrink-0" aria-hidden="true" />}<div><p className="font-semibold">{completenessBlocked ? "Prepared review has unresolved completeness blockers" : completenessOverridden ? "Prepared for human review after reasoned completeness override — not transmitted" : completenessReady ? "Prepared for human review — not transmitted" : "Completeness is not verified — manual review required"}</p><p className="mt-1 text-sm">Decision support only. A provider and coder remain responsible for the final billing decision.</p></div></div></div>
              <EmReview em={result.em} supportedEm={supportedEm} encounter={encounter} dataReview={result.em?.data_review ?? result.data_review} clinicalReview={result.em?.clinical_review ?? result.clinical_review} />
              <DiagnosisSequencingNotice value={result.diagnosis_sequencing} />
              <div className="grid gap-5 lg:grid-cols-2"><CandidateGroup title="ICD-10-CM candidates" candidates={icdCandidates} primaryCode={primaryDiagnosis} primaryLabel={primaryLabel} onCopy={copyText} /><CandidateGroup title="CPT and E/M candidates" candidates={cptCandidates} onCopy={copyText} /></div>
              {result.withheld_without_evidence > 0 && <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">{result.withheld_without_evidence} candidate{result.withheld_without_evidence === 1 ? " was" : "s were"} withheld because verified evidence was unavailable.</div>}
              {result.withheld_without_reason > 0 && <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">{result.withheld_without_reason} held or removed candidate{result.withheld_without_reason === 1 ? " was" : "s were"} withheld because a required review reason was unavailable.</div>}
              <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="copy-heading">
                <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 id="copy-heading" className="text-base font-semibold text-slate-950">Human-controlled handoff</h2><p className="mt-1 text-sm text-slate-600">Copying places text on this device’s clipboard. It does not send anything.</p></div><Pill>{suggested.length} evidence-backed suggestion{suggested.length === 1 ? "" : "s"}</Pill></div>
                <div className="mt-4 flex flex-wrap gap-2">
                  <button type="button" disabled={!safeCopyAll} onClick={() => copyText(safeCopyAll, "Suggested codes copied — not sent.")} className="inline-flex min-h-10 items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium hover:bg-slate-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-50"><Copy size={15} aria-hidden="true" />Copy suggested codes</button>
                  <button type="button" disabled={!preparedBlock} onClick={() => copyText(preparedBlock, "Prepared Athena-friendly block copied — not sent.")} className="inline-flex min-h-10 items-center gap-2 rounded-lg bg-slate-950 px-3 py-2 text-sm font-semibold text-white hover:bg-slate-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700 disabled:cursor-not-allowed disabled:opacity-50"><Copy size={15} aria-hidden="true" />Copy prepared Athena-friendly block</button>
                </div>
                <p className="mt-3 min-h-5 text-sm font-medium text-sky-800" role="status" aria-live="polite">{copyStatus}</p>
                <details className="group mt-3 rounded-xl border border-slate-200 bg-slate-50"><summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-700"><ChevronDown size={15} className="transition group-open:rotate-180" aria-hidden="true" />Preview prepared block</summary><pre className="overflow-auto whitespace-pre-wrap border-t border-slate-200 px-3 py-3 text-xs leading-relaxed text-slate-700">{preparedBlock}</pre></details>
              </section>
              <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-labelledby="provenance-heading">
                <h2 id="provenance-heading" className="text-base font-semibold text-slate-950">Provenance and controls</h2>
                <dl className="mt-3 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
                  <div><dt className="text-xs uppercase tracking-wide text-slate-500">Source</dt><dd className="mt-1">{sourceLabel(result.dataClass || encounter.dataClass)}</dd></div>
                  <div><dt className="text-xs uppercase tracking-wide text-slate-500">Engine / ruleset bundle</dt><dd className="mt-1 break-words font-mono text-xs">{result.engine_version || "Not reported"}</dd></div>
                  <div><dt className="text-xs uppercase tracking-wide text-slate-500">Inference</dt><dd className="mt-1">{result.inferenceMode || "Not reported"}{result.inferenceModel ? ` · ${result.inferenceModel}` : ""}</dd></div>
                  <div><dt className="text-xs uppercase tracking-wide text-slate-500">Clinical note modified</dt><dd className="mt-1 font-semibold text-emerald-800">{result.clinical_note_modified === false ? "No" : "Not confirmed"}</dd></div>
                  <div><dt className="text-xs uppercase tracking-wide text-slate-500">Transmission</dt><dd className="mt-1 font-semibold">None</dd></div>
                  <div><dt className="text-xs uppercase tracking-wide text-slate-500">Human review</dt><dd className="mt-1 font-semibold">Required</dd></div>
                </dl>
              </section>
            </>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center"><ClipboardCheck size={24} className="mx-auto text-slate-400" aria-hidden="true" /><p className="mt-2 font-medium text-slate-950">Ready for provider review</p><p className="mt-1 text-sm text-slate-600">Run the billing review to see evidence-supported recommendations. Billing inference and transmission do not run automatically.</p></div>
          )}
        </>
      )}
    </div>
  );
}
