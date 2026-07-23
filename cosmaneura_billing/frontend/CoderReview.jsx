import React, { useState, useEffect } from "react";
import { Check, X, Pencil, Plus, Clock, ShieldCheck, ChevronDown, Info, Send } from "lucide-react";

/*
  CosmaNeura — Coder review & feedback (prototype)
  ================================================
  Purpose: the engine pre-codes the encounter with evidence; the CODER stays in
  control and reviews faster. Each code shows *why* it was chosen (upskilling),
  the coder accepts / edits / rejects with a reason in a click or two (speed), and
  Submit emits structured feedback that the model-tuning pipeline consumes.

  FEEDBACK CONTRACT — POST to /api/feedback; shape matches coder_feedback.py:
    EncounterFeedback {
      encounter_id, coder_id, engine_version,
      submitted_em, engine_em, final_em,
      decisions: [{ kind, engine_code, action, final_code, reason_code, note }],
      time_spent_seconds, overall_note
    }
    action     ∈ accept | modify | reject | add
    reason_code∈ the Reason.* vocabulary (see REASONS below)

  Replace the mock submit with:
    await fetch("/api/feedback", {method:"POST", headers:{"content-type":"application/json"},
                                  body: JSON.stringify(payload)});
*/

const ENGINE = {
  encounterId: "ENC-100237",
  engineVersion: "cpt2025;icd10-FY2026;ncci-2026Q2;model=opus-4-8",
  submittedEm: "99213",
  coderId: "coder_A",
  em: {
    code: "99214", label: "Office visit, established · moderate MDM",
    why: "Moderate MDM: an undiagnosed problem with uncertain prognosis (problems) plus the decision to refer for diagnostic colonoscopy (risk) meets 2 of 3 at the moderate level. Submitted as 99213, which is below what the note supports.",
    evidence: "refer you for a colonoscopy to be safe",
  },
  dx: [
    { id: "k625", code: "K62.5", label: "Hemorrhage of anus and rectum", primary: true,
      why: "Documented hematochezia; mapped via CCSR to the rectal-hemorrhage code.", evidence: "When I eat spicy food, I poop blood" },
    { id: "k5900", code: "K59.00", label: "Constipation, unspecified",
      why: "Constipation documented in the HPI and addressed in the plan.", evidence: "Sometimes when I have constipation" },
    { id: "r197", code: "R19.7", label: "Diarrhea, unspecified",
      why: "Diarrhea documented in the history.", evidence: "I do have diarrhea often" },
  ],
  withheld: [
    { id: "e669", code: "E66.9", label: "Obesity, unspecified", status: "held",
      why: "BMI 37.6 is in the vitals but obesity isn't addressed in the plan — the engine HELD it. Bill only if it was actually assessed/managed.", evidence: "weigh 255 pounds" },
    { id: "z800", code: "Z80.0", label: "Family history of digestive cancer", status: "rejected",
      why: "The patient explicitly denied family history, so the engine REJECTED this as contradicted.", evidence: "I do not have a family history of colon cancer" },
  ],
};

const EM_OPTIONS = ["99212", "99213", "99214", "99215"];
const REASONS = {
  EM: [
    ["em_doc_supports_higher_level", "Documentation supports a higher level"],
    ["em_doc_supports_lower_level", "Documentation supports a lower level"],
    ["em_mdm_element_disagreement", "Disagree on an MDM element"],
    ["em_time_based_leveling", "Should be time-based"],
    ["other", "Other"],
  ],
  ICD10: [
    ["dx_not_supported_by_doc", "Not supported by documentation"],
    ["dx_contradicted_by_doc", "Contradicted by documentation"],
    ["dx_needs_more_specific_code", "Needs a more specific code"],
    ["dx_primary_secondary_swap", "Wrong primary/secondary"],
    ["other", "Other"],
  ],
  ADD: [["dx_missed_by_engine", "Engine missed this code"]],
};

function Pill({ tone, children }) {
  return <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>{children}</span>;
}

function Why({ text, evidence }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      <button onClick={() => setOpen(!open)} className="inline-flex items-center gap-1 text-xs text-sky-700 hover:underline">
        <Info size={12} /> Why this code <ChevronDown size={12} className={open ? "rotate-180 transition" : "transition"} />
      </button>
      {open && (
        <div className="mt-1 rounded-lg bg-sky-50 p-2.5 text-xs leading-relaxed text-slate-600">
          {text}
          {evidence && <div className="mt-1 italic text-slate-500">Documentation: “{evidence}”</div>}
        </div>
      )}
    </div>
  );
}

const seg = (active, tone) =>
  `inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-medium transition ${active ? tone : "text-slate-500 hover:bg-slate-100"}`;

function CodeRow({ row, kind, decision, set }) {
  const a = decision.action;
  return (
    <div className="border-t border-slate-100 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <code className="w-16 font-mono text-sm text-slate-900">{a === "modify" && decision.final ? decision.final : row.code}</code>
        <span className="flex-1 text-sm text-slate-700">{row.label}{row.primary && <span className="ml-2"><Pill tone="bg-sky-50 text-sky-700 ring-sky-200">primary</Pill></span>}</span>
        <div className="flex items-center gap-1 rounded-lg bg-slate-50 p-0.5">
          <button onClick={() => set({ action: "accept", final: row.code, reason: null })} className={seg(a === "accept", "bg-emerald-100 text-emerald-700")}><Check size={13} />Accept</button>
          <button onClick={() => set({ action: "modify", final: a === "modify" ? decision.final : row.code })} className={seg(a === "modify", "bg-amber-100 text-amber-700")}><Pencil size={13} />Edit</button>
          <button onClick={() => set({ action: "reject", final: null })} className={seg(a === "reject", "bg-red-100 text-red-700")}><X size={13} />Reject</button>
        </div>
      </div>
      <Why text={row.why} evidence={row.evidence} />
      {a === "modify" && (
        <div className="mt-2 flex flex-wrap items-center gap-2 pl-16">
          {kind === "EM" ? (
            <select value={decision.final || row.code} onChange={(e) => set({ ...decision, action: "modify", final: e.target.value })}
              className="rounded-md border border-slate-300 px-2 py-1 text-sm">
              {EM_OPTIONS.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          ) : (
            <input value={decision.final || ""} onChange={(e) => set({ ...decision, action: "modify", final: e.target.value })}
              placeholder="corrected code" className="w-32 rounded-md border border-slate-300 px-2 py-1 text-sm font-mono" />
          )}
          <ReasonSelect kind={kind} value={decision.reason} onChange={(r) => set({ ...decision, action: "modify", reason: r })} />
        </div>
      )}
      {a === "reject" && (
        <div className="mt-2 pl-16">
          <ReasonSelect kind={kind} value={decision.reason} onChange={(r) => set({ ...decision, action: "reject", reason: r })} />
        </div>
      )}
    </div>
  );
}

function ReasonSelect({ kind, value, onChange }) {
  return (
    <select value={value || ""} onChange={(e) => onChange(e.target.value)} className="rounded-md border border-slate-300 px-2 py-1 text-sm text-slate-700">
      <option value="" disabled>reason…</option>
      {(REASONS[kind] || REASONS.ICD10).map(([v, label]) => <option key={v} value={v}>{label}</option>)}
    </select>
  );
}

export default function App() {
  const init = {};
  [ENGINE.em, ...ENGINE.dx].forEach((r, i) => { init[i === 0 ? "em" : ENGINE.dx[i - 1].id] = { action: "accept", final: r.code, reason: null }; });
  const [dec, setDec] = useState(init);
  const [withheld, setWithheld] = useState(() => Object.fromEntries(ENGINE.withheld.map((w) => [w.id, { bill: false, reason: null }])));
  const [added, setAdded] = useState([]);
  const [seconds, setSeconds] = useState(0);
  const [done, setDone] = useState(null);

  useEffect(() => { const t = setInterval(() => setSeconds((s) => s + 1), 1000); return () => clearInterval(t); }, []);

  const set = (key, val) => setDec((d) => ({ ...d, [key]: val }));
  const captured = [{ k: "EM", id: "em", row: ENGINE.em }, ...ENGINE.dx.map((r) => ({ k: "ICD10", id: r.id, row: r }))];
  const agreed = captured.filter(({ id }) => dec[id].action === "accept").length;

  const submit = () => {
    const decisions = [];
    decisions.push({ kind: "EM", engine_code: ENGINE.em.code, action: dec.em.action, final_code: dec.em.final, reason_code: dec.em.reason, note: "" });
    ENGINE.dx.forEach((r) => { const d = dec[r.id]; decisions.push({ kind: "ICD10", engine_code: r.code, action: d.action, final_code: d.final, reason_code: d.reason, note: "" }); });
    ENGINE.withheld.forEach((w) => { const s = withheld[w.id]; if (s.bill) decisions.push({ kind: "ICD10", engine_code: w.code, action: "add", final_code: w.code, reason_code: s.reason || "dx_missed_by_engine", note: `coder billed engine-${w.status} code` }); else decisions.push({ kind: "ICD10", engine_code: w.code, action: "accept", final_code: null, note: `confirmed not billed (engine ${w.status})` }); });
    added.forEach((a) => { if (a.code) decisions.push({ kind: "ICD10", engine_code: null, action: "add", final_code: a.code, reason_code: "dx_missed_by_engine", note: a.note || "" }); });
    const payload = {
      encounter_id: ENGINE.encounterId, coder_id: ENGINE.coderId, engine_version: ENGINE.engineVersion,
      submitted_em: ENGINE.submittedEm, engine_em: ENGINE.em.code, final_em: dec.em.final,
      decisions, time_spent_seconds: seconds, overall_note: "",
    };
    setDone(payload);
  };

  return (
    <div className="min-h-screen bg-slate-50 px-4 py-6 text-slate-900" style={{ fontFamily: "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial" }}>
      <div className="mx-auto max-w-3xl">
        <header className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-900 text-white"><ShieldCheck size={20} /></span>
            <div>
              <h1 className="text-lg font-medium tracking-tight">Coder review · {ENGINE.encounterId}</h1>
              <p className="text-xs text-slate-500">You're in control — the engine pre-coded this from the documentation. Review, correct, submit.</p>
            </div>
          </div>
          <Pill tone="bg-slate-100 text-slate-600 ring-slate-300"><Clock size={12} className="mr-1" />{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}</Pill>
        </header>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-slate-900">Suggested claim</p>
            <span className="text-xs text-slate-500">submitted as <code className="font-mono">{ENGINE.submittedEm}</code> · engine suggests <code className="font-mono">{ENGINE.em.code}</code></span>
          </div>

          <p className="mt-4 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">E/M level</p>
          <CodeRow row={ENGINE.em} kind="EM" decision={dec.em} set={(v) => set("em", v)} />

          <p className="mt-4 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Diagnoses</p>
          {ENGINE.dx.map((r) => <CodeRow key={r.id} row={r} kind="ICD10" decision={dec[r.id]} set={(v) => set(r.id, v)} />)}

          <p className="mt-5 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Engine withheld these — your call</p>
          {ENGINE.withheld.map((w) => (
            <div key={w.id} className="border-t border-slate-100 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <code className="w-16 font-mono text-sm text-slate-900">{w.code}</code>
                <span className="flex-1 text-sm text-slate-700">{w.label}</span>
                <Pill tone={w.status === "held" ? "bg-amber-50 text-amber-700 ring-amber-200" : "bg-red-50 text-red-700 ring-red-200"}>{w.status}</Pill>
                <div className="flex items-center gap-1 rounded-lg bg-slate-50 p-0.5">
                  <button onClick={() => setWithheld((s) => ({ ...s, [w.id]: { ...s[w.id], bill: false } }))} className={seg(!withheld[w.id].bill, "bg-slate-200 text-slate-700")}>Don't bill</button>
                  <button onClick={() => setWithheld((s) => ({ ...s, [w.id]: { ...s[w.id], bill: true } }))} className={seg(withheld[w.id].bill, "bg-emerald-100 text-emerald-700")}>Bill it</button>
                </div>
              </div>
              <Why text={w.why} evidence={w.evidence} />
            </div>
          ))}

          <p className="mt-5 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Add a code the engine missed</p>
          {added.map((a, i) => (
            <div key={i} className="mb-2 flex items-center gap-2">
              <input value={a.code} onChange={(e) => setAdded((arr) => arr.map((x, j) => j === i ? { ...x, code: e.target.value } : x))} placeholder="ICD-10 code" className="w-32 rounded-md border border-slate-300 px-2 py-1 text-sm font-mono" />
              <input value={a.note} onChange={(e) => setAdded((arr) => arr.map((x, j) => j === i ? { ...x, note: e.target.value } : x))} placeholder="why it was missed" className="flex-1 rounded-md border border-slate-300 px-2 py-1 text-sm" />
              <button onClick={() => setAdded((arr) => arr.filter((_, j) => j !== i))} className="text-slate-400 hover:text-red-500"><X size={16} /></button>
            </div>
          ))}
          <button onClick={() => setAdded((arr) => [...arr, { code: "", note: "" }])} className="inline-flex items-center gap-1 text-sm text-sky-700 hover:underline"><Plus size={14} />Add code</button>

          <div className="mt-6 flex items-center justify-between border-t border-slate-100 pt-4">
            <p className="text-xs text-slate-500">You accepted {agreed} of {captured.length} suggestions unchanged. Edits and rejections teach the engine.</p>
            <button onClick={submit} className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 active:scale-95"><Send size={15} />Submit &amp; next</button>
          </div>
        </div>

        {done && (
          <div className="mt-5 rounded-2xl border border-emerald-200 bg-white p-5 shadow-sm">
            <div className="flex items-center gap-2 text-sm font-medium text-emerald-800"><Check size={18} />Feedback recorded — final E/M {done.final_em}, {done.decisions.length} decisions, {done.time_spent_seconds}s.</div>
            <p className="mt-2 text-xs text-slate-500">This payload posts to <code className="font-mono">/api/feedback</code>; the tuning pipeline turns it into a gold row and a ranked rule/prompt backlog (see analyze_feedback.py).</p>
            <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">{JSON.stringify(done, null, 2)}</pre>
          </div>
        )}
      </div>
    </div>
  );
}
