import React, { useEffect, useState } from "react";
import {
  Stethoscope, FileText, ShieldCheck, AlertTriangle, Check, X, Clock,
  ArrowUp, Heart, RefreshCw,
} from "lucide-react";
import { api } from "./api.js";

/*
  Provider card + biller worksheet, wired to the live engine:
    GET  /api/encounters/{id}   -> populate the editable documentation
    POST /api/billing           -> run the 5-agent pipeline on what's shown
  When the engine is unreachable (`live` prop false) the demo encounter and a
  canned result keep the page walkable.
*/

const DEMO_ENCOUNTER = {
  encounterId: "ENC-100237",
  submittedEm: "99213",
  intake:
    "Hi doctor, I am a 26 year old male. I am 5 feet and 9 inches tall and weigh 255 pounds. " +
    "When I eat spicy food, I poop blood. Sometimes when I have constipation as well, I poop a " +
    "little bit of blood. I am really scared that I have colon cancer. I do have diarrhea often. " +
    "I do not have a family history of colon cancer. I got blood tests done last night.",
  transcript:
    "Provider: Tell me about the bleeding — how much, what color, how often?\n" +
    "Patient: Bright red, a little, mostly after spicy food or when constipated. A few times a month.\n" +
    "Provider: Any weight loss or other changes?\n" +
    "Patient: I've lost a bit of weight and I get diarrhea pretty often.\n" +
    "Provider: I reviewed the CBC you brought — reassuring, no anemia. Given the bleeding and weight " +
    "change, I'll order a stool occult blood test and refer you for a colonoscopy to be safe.",
  signedNote:
    "26 y/o male, established patient. CC: intermittent hematochezia. HPI: bright red blood per rectum, " +
    "small volume, with spicy food and constipation; frequent diarrhea; mild weight loss. Denies family " +
    "history of colorectal cancer. Data: reviewed outside CBC (no anemia). Ordered FOBT. Vitals: 69in, " +
    "255lb, BMI 37.6. A/P: (1) Hematochezia — refer for colonoscopy, FOBT today; (2) Constipation — fiber, " +
    "hydration; (3) Diarrhea — monitor. Electronically signed.",
};

// Offline fallback — the result the engine produces for the demo encounter.
function mockBillingApi() {
  return {
    encounterId: "ENC-100237",
    summary: "Rectal bleeding with bowel changes; patient anxious about colon cancer.",
    captured: [
      { kind: "EM", code: "99214", label: "Office visit, established · moderate MDM",
        disposition: "captured", denialRisk: "low", correctedFrom: "99213",
        evidence: [{ source: "transcript", quote: "refer you for a colonoscopy to be safe" }] },
      { kind: "ICD10", code: "K62.5", label: "Hemorrhage of anus and rectum", primary: true,
        disposition: "captured", denialRisk: "low",
        evidence: [{ source: "intake", quote: "When I eat spicy food, I poop blood" }] },
      { kind: "ICD10", code: "K59.00", label: "Constipation, unspecified",
        disposition: "captured", denialRisk: "medium",
        evidence: [{ source: "intake", quote: "Sometimes when I have constipation" }] },
      { kind: "ICD10", code: "R19.7", label: "Diarrhea, unspecified",
        disposition: "captured", denialRisk: "medium",
        evidence: [{ source: "intake", quote: "I do have diarrhea often" }] },
    ],
    declined: [
      { kind: "ICD10", code: "Z80.0", label: "Family history of malignant neoplasm of digestive organs",
        disposition: "rejected", denialRisk: "unknown", evidence: [],
        reason: "Patient explicitly denied family history of colorectal cancer." },
      { kind: "ICD10", code: "E66.8", label: "Other obesity",
        disposition: "needs_review", denialRisk: "unknown", evidence: [],
        reason: "BMI 37.6 is documented in vitals but obesity is not addressed in the plan." },
    ],
    em: { code: "99214", label: "Office visit, established · moderate MDM" },
    primary: { code: "K62.5", label: "Hemorrhage of anus and rectum" },
    diagnoses: [
      { code: "K62.5", label: "Hemorrhage of anus and rectum", denialRisk: "low" },
      { code: "K59.00", label: "Constipation, unspecified", denialRisk: "medium" },
      { code: "R19.7", label: "Diarrhea, unspecified", denialRisk: "medium" },
    ],
    denialFlags: [
      "K59.00 & R19.7 are low-specificity — add type/acuity/cause where known to support a more specific code.",
    ],
    requiresSignoff: true,
    audit: [
      { action: "CAPTURE", code: "99214", detail: "within MDM ceiling, evidence attached" },
      { action: "CAPTURE", code: "K62.5", detail: "linked to documented finding" },
      { action: "REJECT", code: "Z80.0", detail: "family history denied by patient" },
      { action: "HOLD", code: "E66.8", detail: "documented, not addressed in plan" },
    ],
    feedback: [
      { kind: "coding_education", target: "provider", tone: "subtle",
        message: "Submitted as 99213, but the documentation already supports 99214. No additional work — the level reflects the visit you documented.",
        patientBenefit: "Accurate coding of work already performed; no change to care." },
      { kind: "documentation_gap", target: "provider", tone: "subtle",
        message: "BMI 37.6 is in the vitals but obesity isn't addressed in the plan; it's reportable only if you assess/manage it.",
        patientBenefit: "Complete documentation reflects the care actually delivered." },
    ],
    revenueIntegrityNote:
      "4 codes captured (3 dx + E/M), each linked to documentation. 1 rejected as unsupported, 1 held for sign-off. No code exceeds its MDM ceiling.",
  };
}

const RISK = {
  low: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  medium: "bg-amber-50 text-amber-700 ring-amber-200",
  high: "bg-red-50 text-red-700 ring-red-200",
  unknown: "bg-slate-100 text-slate-500 ring-slate-300",
};

function Pill({ tone, children }) {
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      {children}
    </span>
  );
}

function SectionLabel({ children }) {
  return <p className="mt-5 mb-2 text-xs font-medium uppercase tracking-wide text-slate-400">{children}</p>;
}

function ProviderCard({ r }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center gap-2.5">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-sky-50 text-sky-700">
          <Stethoscope size={18} />
        </span>
        <div>
          <p className="text-sm font-medium text-slate-900">Provider view</p>
          <p className="text-xs text-slate-500">post-visit · {r.encounterId}</p>
        </div>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-slate-600">{r.summary}</p>

      <SectionLabel>Captured this visit</SectionLabel>
      <div className="divide-y divide-slate-100">
        {r.captured.map((c) => (
          <div key={c.code} className="py-2.5">
            <div className="flex flex-wrap items-baseline gap-2">
              <code className="w-16 font-mono text-sm text-slate-900">{c.code}</code>
              <span className="flex-1 text-sm text-slate-700">{c.label}</span>
              {c.denialPrior != null && (
                <span className="text-xs text-slate-400" title="Medicare denial rate for this code (CMS PSPS 2024): pilot state, then national">
                  {c.denialPriorState
                    ? `denials ${c.denialPriorState.state} ${(c.denialPriorState.rate * 100).toFixed(1)}% · natl ${(c.denialPrior * 100).toFixed(1)}%`
                    : `natl denial ${(c.denialPrior * 100).toFixed(1)}%`}
                </span>
              )}
              {c.correctedFrom && (
                <Pill tone={RISK.low}><ArrowUp size={12} className="mr-0.5" />from {c.correctedFrom}</Pill>
              )}
              {c.primary && <Pill tone="bg-sky-50 text-sky-700 ring-sky-200">primary</Pill>}
            </div>
            {c.evidence[0] && (
              <p className="mt-1 pl-16 text-xs italic text-slate-500">“{c.evidence[0].quote}”</p>
            )}
          </div>
        ))}
      </div>

      <SectionLabel>Worth a look — no extra work</SectionLabel>
      {r.feedback.filter((f) => f.kind === "coding_education" || f.kind === "documentation_gap").map((f, i) => (
        <div key={i} className="mb-2 flex gap-2 text-sm text-slate-700">
          {f.kind === "coding_education"
            ? <ArrowUp size={16} className="mt-0.5 shrink-0 text-emerald-600" />
            : <FileText size={16} className="mt-0.5 shrink-0 text-slate-400" />}
          <span>{f.message}</span>
        </div>
      ))}

      <SectionLabel>For the patient</SectionLabel>
      {r.feedback.filter((f) => f.kind === "care_quality").map((f, i) => (
        <div key={i} className="mb-2 flex gap-2 text-sm text-slate-700">
          <Heart size={16} className="mt-0.5 shrink-0 text-sky-600" />
          <span>{f.message}</span>
        </div>
      ))}

      <SectionLabel>Not billed — transparency</SectionLabel>
      {r.declined.map((c) => (
        <div key={c.code} className="mb-2 flex gap-2 text-sm text-slate-700">
          {c.disposition === "rejected"
            ? <X size={16} className="mt-0.5 shrink-0 text-red-500" />
            : <Clock size={16} className="mt-0.5 shrink-0 text-amber-500" />}
          <span><code className="font-mono text-xs">{c.code}</code> — {c.reason}</span>
        </div>
      ))}
    </div>
  );
}

function BillerCard({ r }) {
  const actionTone = {
    CAPTURE: "text-emerald-600", REJECT: "text-red-500", HOLD: "text-amber-600",
    DOWNGRADE: "text-amber-600", "EVIDENCE-REMOVED": "text-red-500",
    "EVIDENCE-RELABELED": "text-slate-500", CPT: "text-slate-500",
  };
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center gap-2.5">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
          <FileText size={18} />
        </span>
        <div className="flex-1">
          <p className="text-sm font-medium text-slate-900">Biller worksheet</p>
          <p className="text-xs text-slate-500">claim-ready · {r.encounterId}</p>
        </div>
        {r.requiresSignoff && <Pill tone={RISK.medium}><Clock size={12} className="mr-1" />needs sign-off</Pill>}
      </div>

      <SectionLabel>Claim lines</SectionLabel>
      <div className="flex items-center justify-between border-t border-slate-100 py-2">
        <span className="text-sm text-slate-500">E/M code</span>
        <span className="text-right">
          <code className="font-mono text-sm text-slate-900">{r.em?.code}</code>
          {r.em?.label && <span className="ml-2 text-xs text-slate-500">{r.em.label}</span>}
        </span>
      </div>
      <div className="flex items-center justify-between border-t border-slate-100 py-2">
        <span className="text-sm text-slate-500">Primary diagnosis</span>
        <code className="font-mono text-sm text-slate-900">{r.primary?.code}</code>
      </div>

      <SectionLabel>Diagnoses (claim order)</SectionLabel>
      <div className="divide-y divide-slate-100">
        {r.diagnoses.map((d) => (
          <div key={d.code} className="flex items-center gap-2 py-2">
            <code className="w-16 font-mono text-sm text-slate-900">{d.code}</code>
            <span className="flex-1 text-sm text-slate-700">{d.label}</span>
            <Pill tone={RISK[d.denialRisk] || RISK.unknown}>denial: {d.denialRisk}</Pill>
          </div>
        ))}
      </div>

      <SectionLabel>Denial-risk flags</SectionLabel>
      {r.denialFlags.length === 0 && <p className="text-sm text-slate-400">none</p>}
      {r.denialFlags.map((f, i) => (
        <div key={i} className="mb-2 flex gap-2 text-sm text-slate-700">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
          <span>{f}</span>
        </div>
      ))}

      <SectionLabel>Audit trail</SectionLabel>
      <div className="rounded-lg bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-600">
        {r.audit.map((a, i) => (
          <div key={i}>
            <span className={`font-medium ${actionTone[a.action] || "text-slate-500"}`}>{a.action}</span>{" "}
            <span className="text-slate-900">{a.code}</span> — {a.detail}
          </div>
        ))}
      </div>
    </div>
  );
}

function Field({ label, value, onChange, rows = 3 }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-400">{label}</span>
      <textarea
        value={value} onChange={(e) => onChange(e.target.value)} rows={rows}
        className="w-full resize-y rounded-lg border border-slate-200 bg-white p-2.5 text-sm text-slate-700 shadow-sm outline-none focus:border-slate-400 focus:ring-2 focus:ring-slate-200"
      />
    </label>
  );
}

export default function BillingReview({ encounterId, live }) {
  const [enc, setEnc] = useState(DEMO_ENCOUNTER);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // load the selected encounter's documentation into the editor
  useEffect(() => {
    setResult(null);
    setError(null);
    if (!live) { setEnc(DEMO_ENCOUNTER); return; }
    (async () => {
      try {
        const e = await api(`/api/encounters/${encodeURIComponent(encounterId)}`);
        setEnc({
          encounterId: e.id, submittedEm: e.submittedEm || "",
          intake: e.intake || "", transcript: e.transcript || "", signedNote: e.signedNote || "",
        });
      } catch {
        setEnc(DEMO_ENCOUNTER);
      }
    })();
  }, [encounterId, live]);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      if (live) {
        const j = await api("/api/billing", {
          method: "POST",
          body: {
            encounterId: enc.encounterId, submittedEm: enc.submittedEm || null,
            intake: enc.intake, transcript: enc.transcript, signedNote: enc.signedNote,
          },
        });
        setResult(j);
      } else {
        await new Promise((r) => setTimeout(r, 500));
        setResult(mockBillingApi());
      }
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="mb-4 flex items-center justify-between">
          <p className="text-sm font-medium text-slate-900">Encounter {enc.encounterId}</p>
          <span className="text-xs text-slate-500">
            {enc.submittedEm ? <>submitted as <code className="font-mono">{enc.submittedEm}</code></> : "no code submitted yet"}
          </span>
        </div>
        <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
          <Field label="Intake chatbot text" value={enc.intake} onChange={(v) => setEnc({ ...enc, intake: v })} rows={6} />
          <Field label="Visit transcript" value={enc.transcript} onChange={(v) => setEnc({ ...enc, transcript: v })} rows={6} />
          <Field label="Signed note" value={enc.signedNote} onChange={(v) => setEnc({ ...enc, signedNote: v })} rows={6} />
        </div>
        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={run} disabled={loading}
            className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 active:scale-95 disabled:opacity-60"
          >
            {loading ? <RefreshCw size={16} className="animate-spin" /> : <Check size={16} />}
            {loading ? "Reviewing…" : "Process encounter"}
          </button>
          <span className="text-xs text-slate-400">Runs the 5-agent pipeline · guardrail can only downgrade or remove</span>
        </div>
        {error && (
          <p className="mt-3 rounded-lg bg-red-50 p-2.5 text-sm text-red-700">engine error: {error}</p>
        )}
      </div>

      {result && (
        <>
          <div className="mt-5 flex items-start gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">
            <ShieldCheck size={18} className="mt-0.5 shrink-0" />
            <span>{result.revenueIntegrityNote}</span>
          </div>
          <div className="mt-5 grid grid-cols-1 gap-5 lg:grid-cols-2">
            <ProviderCard r={result} />
            <BillerCard r={result} />
          </div>

          <div className="mt-5 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-100 text-slate-700">
                <RefreshCw size={18} />
              </span>
              <div>
                <p className="text-sm font-medium text-slate-900">Feedback loop</p>
                <p className="text-xs text-slate-500">routed to the platform · every nudge states a patient benefit</p>
              </div>
            </div>
            <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">
              {result.feedback.map((f, i) => (
                <div key={i} className="rounded-xl border border-slate-200 bg-slate-50 p-3">
                  <div className="mb-1 flex items-center gap-2">
                    <Pill tone="bg-slate-100 text-slate-600 ring-slate-300">{f.kind.replace(/_/g, " ")}</Pill>
                    <span className="text-xs text-slate-400">→ {f.target.replace(/_/g, " ")}</span>
                  </div>
                  <p className="text-sm text-slate-700">{f.message}</p>
                  <p className="mt-1 flex items-start gap-1 text-xs text-slate-500">
                    <Heart size={12} className="mt-0.5 shrink-0 text-sky-500" />{f.patientBenefit}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </>
  );
}
