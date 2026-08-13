import React, { useEffect, useState } from "react";
import {
  ArrowUp, Check, ClipboardCopy, ClipboardList, DollarSign, FileWarning,
  Quote, ShieldCheck, X,
} from "lucide-react";
import { api } from "./api.js";

/*
  F1 — the provider Billing Tab (synthesis §F1): the same visit, reorganized
  into a billing reference view. The clinical note is never altered — this
  view only renders GET /api/encounters/{id}/billing-tab.

  Rendering rules the contract guarantees and this view leans on:
    * every candidate arrives with ≥1 evidence span (no span, no render);
    * held/removed candidates always carry a status_reason;
    * a multi-code finding arrives once, with `alternates`;
    * copy affordances are per-code and copy-all (removed codes never in
      copy-all).
  Microcopy discipline: documented, medically necessary care — never revenue
  framing. Compact by default (Scott 7/21: outputs were "a little too much");
  evidence expands on demand.
*/

const STATUS_TONE = {
  suggested: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  held: "bg-amber-50 text-amber-700 ring-amber-200",
  removed: "bg-rose-50 text-rose-600 ring-rose-200",
};
const STATUS_LABEL = { suggested: "suggested", held: "held", removed: "removed" };

function Pill({ tone, children, title }) {
  return (
    <span title={title}
          className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      {children}
    </span>
  );
}

function SectionLabel({ children }) {
  return <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-400">{children}</p>;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  }
}

function CopyButton({ text, id, copied, setCopied, label = "Copy" }) {
  const done = copied === id;
  return (
    <button
      onClick={async () => {
        if (await copyText(text)) {
          setCopied(id);
          // clear only OUR indicator — copying a second code within the
          // window must not wipe the newer confirmation
          setTimeout(() => setCopied((cur) => (cur === id ? null : cur)), 1400);
        }
      }}
      className={`inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-medium ring-1 ring-inset transition ${
        done ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
             : "bg-white text-slate-600 ring-slate-200 hover:bg-slate-50"}`}>
      {done ? <Check size={12} /> : <ClipboardCopy size={12} />}
      {done ? "Copied" : label}
    </button>
  );
}

function EvidenceSpans({ spans }) {
  return (
    <ul className="mt-1 space-y-0.5">
      {spans.map((s, i) => (
        <li key={i} className="flex items-start gap-1.5 text-xs text-slate-500">
          <Quote size={11} className="mt-0.5 shrink-0 text-slate-300" />
          <span>
            “{s.text}”
            <span className="ml-1 rounded bg-slate-100 px-1 py-px text-[10px] uppercase tracking-wide text-slate-400">{s.source}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

function CandidateRow({ c, copied, setCopied }) {
  const [open, setOpen] = useState(c.status !== "suggested");
  return (
    <li className={`rounded-xl border px-3 py-2.5 ${c.status === "removed" ? "border-slate-100 bg-slate-50/60" : "border-slate-200 bg-white"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={STATUS_TONE[c.status]}>
          {c.status === "suggested" && <Check size={11} />}
          {c.status === "held" && <FileWarning size={11} />}
          {c.status === "removed" && <X size={11} />}
          {STATUS_LABEL[c.status]}
        </Pill>
        <code className={`text-sm font-semibold ${c.status === "removed" ? "text-slate-400 line-through" : "text-slate-900"}`}>{c.code}</code>
        <span className={`text-sm ${c.status === "removed" ? "text-slate-400" : "text-slate-600"}`}>{c.description}</span>
        <span className="ml-auto flex items-center gap-1.5">
          <button onClick={() => setOpen(!open)}
                  className="text-xs text-slate-400 underline-offset-2 hover:underline">
            {open ? "less" : "why"}
          </button>
          {c.status === "suggested" && (
            <CopyButton text={c.copy_text} id={`code-${c.code}`} copied={copied} setCopied={setCopied} />
          )}
        </span>
      </div>

      {(c.alternates || []).length > 0 && (
        <p className="mt-1 text-xs text-slate-500">
          Also maps to:{" "}
          {(c.alternates || []).map((a) => <code key={a} className="mr-1 rounded bg-slate-100 px-1">{a}</code>)}
          <span className="text-slate-400">(same finding — rendered once)</span>
        </p>
      )}
      {c.status !== "suggested" && c.status_reason && (
        <p className="mt-1 text-xs text-slate-500">{c.status_reason}</p>
      )}
      {open && (
        <>
          <EvidenceSpans spans={c.evidence_spans || []} />
          {c.coverage_note && <p className="mt-1 text-xs text-sky-700">Coverage: {c.coverage_note}</p>}
          {(c.linked_plan_items || []).length > 0 && (
            <p className="mt-1 text-xs text-slate-500">
              <span className="font-medium text-slate-600">Plan under this code:</span>{" "}
              {(c.linked_plan_items || []).join(" · ")}
            </p>
          )}
        </>
      )}
    </li>
  );
}

function EMCard({ em, copied, setCopied }) {
  const [showFacts, setShowFacts] = useState(false);
  if (!em || !em.suggested_level) return null;
  const facts = em.mdm_facts || [];
  const held = em.status && em.status !== "suggested";
  // Only a code that actually cleared the guardrail may claim the
  // documentation supports it — a held E/M is a question, not a finding.
  const recovered = !held && em.provider_selected_level &&
    em.suggested_level > em.provider_selected_level;
  return (
    <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
      <SectionLabel>E/M level — computed from documented MDM</SectionLabel>
      <div className="flex flex-wrap items-center gap-3">
        <code className="text-2xl font-semibold tracking-tight">{em.suggested_level}</code>
        {recovered && (
          <Pill tone="bg-emerald-50 text-emerald-700 ring-emerald-200"
                title="The documentation already supports this level — evidence below.">
            <ArrowUp size={11} />documentation supports {em.suggested_level} (selected {em.provider_selected_level})
          </Pill>
        )}
        {held && (
          <Pill tone={STATUS_TONE[em.status] || STATUS_TONE.held}
                title={em.status_reason || "Provider confirmation required."}>
            <FileWarning size={11} />
            {em.status} — provider confirmation needed
          </Pill>
        )}
        {!recovered && !held && em.provider_selected_level && (
          <Pill tone="bg-slate-100 text-slate-500 ring-slate-300">selected {em.provider_selected_level}</Pill>
        )}
        <Pill tone="bg-slate-100 text-slate-500 ring-slate-300" title="Time-based pathway pending (Aug 13).">
          MDM 2-of-3
        </Pill>
        <span className="ml-auto">
          {em.status === "suggested" && (
            <CopyButton text={em.suggested_level} id="em" copied={copied} setCopied={setCopied} />
          )}
        </span>
      </div>
      <button onClick={() => setShowFacts(!showFacts)}
              className="mt-2 text-xs text-slate-400 underline-offset-2 hover:underline">
        {showFacts ? "hide the facts behind this level" : `show the ${facts.length} documented facts behind this level`}
      </button>
      {showFacts && (
        <ul className="mt-2 space-y-1.5">
          {facts.map((f, i) => (
            <li key={i} className="flex items-start gap-2 text-xs">
              <span className="rounded bg-slate-100 px-1.5 py-0.5 font-medium uppercase tracking-wide text-slate-500">{f.element}</span>
              <span className="text-slate-600">
                {f.finding}
                {f.evidence_span && <span className="ml-1 text-slate-400">— “{f.evidence_span.text}”</span>}
              </span>
            </li>
          ))}
          {(em.guardrail_actions || []).map((a, i) => (
            <li key={`g${i}`} className="flex items-start gap-2 text-xs text-slate-500">
              <ShieldCheck size={12} className="mt-0.5 shrink-0 text-slate-400" />{a}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CompletenessCard({ comp }) {
  if (!comp || !comp.results || comp.results.length === 0) return null;
  const tone = { present: "text-emerald-600", missing: "text-rose-600", ambiguous: "text-amber-600" };
  return (
    <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
      <SectionLabel>Note completeness — {comp.rulepack}</SectionLabel>
      <ul className="space-y-1">
        {comp.results.map((r) => (
          <li key={r.rule_id} className="flex items-start gap-2 text-xs">
            <span className={`font-semibold ${tone[r.status] || "text-slate-500"}`}>{r.status}</span>
            <span className="text-slate-600">
              {r.rule_id.split(".")[1]}
              {r.evidence ? <span className="text-slate-400"> — “{r.evidence.text}”</span>
                          : <span className="text-slate-400"> — {r.fix_hint}</span>}
            </span>
          </li>
        ))}
      </ul>
      {comp.attestation_block && (
        <pre className="mt-2 whitespace-pre-wrap rounded-lg bg-slate-50 p-2 text-[11px] text-slate-600 ring-1 ring-slate-100">{comp.attestation_block}</pre>
      )}
    </div>
  );
}

export default function BillingTab({ encounterId, live }) {
  const [view, setView] = useState(null);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(null);

  useEffect(() => {
    setView(null); setError(null);
    if (!live) return;
    // Each request runs the full engine pipeline, so latency varies; without
    // this guard a slow earlier encounter can land after a newer one and show
    // the wrong visit's codes.
    let stale = false;
    api(`/api/encounters/${encounterId}/billing-tab`)
      .then((v) => { if (!stale) setView(v); })
      .catch((e) => { if (!stale) setError(String(e)); });
    return () => { stale = true; };
  }, [encounterId, live]);

  if (!live) {
    return (
      <div className="rounded-2xl bg-white p-6 text-sm text-slate-500 shadow-sm ring-1 ring-slate-200">
        The billing tab renders live engine output — start the engine
        (<code className="rounded bg-slate-100 px-1">uvicorn cosmaneura_billing.api:app --port 8000</code>) and reload.
      </div>
    );
  }
  if (error) return <div className="rounded-2xl bg-rose-50 p-4 text-sm text-rose-700 ring-1 ring-rose-200">{error}</div>;
  if (!view) return <div className="p-6 text-sm text-slate-400">Running the engine pipeline…</div>;

  const icd = view.icd_candidates || [];
  const cpt = view.cpt_candidates || [];

  return (
    <div className="space-y-4">
      <EMCard em={view.em} copied={copied} setCopied={setCopied} />

      <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>Diagnosis candidates</SectionLabel>
          {view.copy_all_text && (
            <CopyButton text={view.copy_all_text} id="all" copied={copied}
                        setCopied={setCopied} label="Copy all suggested" />
          )}
        </div>
        <ul className="space-y-2">
          {icd.map((c) => <CandidateRow key={c.code} c={c} copied={copied} setCopied={setCopied} />)}
        </ul>
      </div>

      {cpt.length > 0 && (
        <div className="rounded-2xl bg-white p-4 shadow-sm ring-1 ring-slate-200">
          <SectionLabel>Procedure candidates</SectionLabel>
          <ul className="space-y-2">
            {cpt.map((c) => <CandidateRow key={c.code} c={c} copied={copied} setCopied={setCopied} />)}
          </ul>
        </div>
      )}

      <CompletenessCard comp={view.completeness} />

      <p className="flex items-center justify-center gap-1 text-center text-xs text-slate-400">
        <DollarSign size={12} />
        Captures documented, medically necessary care — codes never exceed what the documentation supports.
        <span className="text-slate-300">· engine {view.engine_version}</span>
      </p>
    </div>
  );
}
