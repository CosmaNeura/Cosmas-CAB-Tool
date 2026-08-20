import React, { useState, useEffect, useRef } from "react";
import { Check, X, Pencil, Plus, Clock, ChevronDown, Info, Send, RefreshCw } from "lucide-react";
import { api } from "./api.js";

/*
  Coder review & feedback, wired to the live engine:
    GET  /api/encounters/{id}/coder-view  -> the pre-coded claim with evidence
    POST /api/feedback                    -> the coder's durable decisions
  Offline (`live` false) it falls back to the demo claim so the flow stays
  walkable.
*/

const FALLBACK_ENGINE = {
  encounterId: "ENC-100237",
  engineVersion: "offline-demo",
  submittedEm: "99213",
  coderId: "coder_A",
  em: {
    code: "99214", label: "Office visit, established · moderate MDM",
    why: "Moderate MDM: an undiagnosed problem with uncertain prognosis (problems) plus the decision to refer for diagnostic colonoscopy (risk) meets 2 of 3 at the moderate level.",
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
    { id: "e668", code: "E66.8", label: "Other obesity", status: "held",
      why: "BMI 37.6 is in the vitals but obesity isn't addressed in the plan — the engine HELD it.", evidence: "weigh 255 pounds" },
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

function Why({ text, evidence, history }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-1">
      {history && (
        <p className="mb-1 inline-flex items-center gap-1 rounded-md bg-violet-50 px-2 py-0.5 text-xs text-violet-700 ring-1 ring-inset ring-violet-200">
          <Info size={11} /> {history}
        </p>
      )}
      <div>
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
      <Why text={row.why} evidence={row.evidence} history={row.history} />
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

export default function CoderReview({ encounterId, live }) {
  const [engine, setEngine] = useState(null);
  const [dec, setDec] = useState({});
  const [withheld, setWithheld] = useState({});
  const [added, setAdded] = useState([]);
  const [seconds, setSeconds] = useState(0);
  const [done, setDone] = useState(null);
  const [error, setError] = useState(null);
  const reviewId = useRef(null);
  const pendingPayload = useRef(null);

  // load the pre-coded claim for this encounter
  useEffect(() => {
    setEngine(null); setDone(null); setError(null); setAdded([]); setSeconds(0);
    // P3 DATA SEAM: one stable retry key per loaded review draft. Keep it until
    // the server acknowledges the submission so response loss cannot double
    // count a preference signal.
    reviewId.current = globalThis.crypto.randomUUID();
    pendingPayload.current = null;
    (async () => {
      let eng = live ? null : FALLBACK_ENGINE;
      if (live) {
        try {
          eng = await api(`/api/encounters/${encodeURIComponent(encounterId)}/coder-view`);
        } catch (e) {
          setError(`could not load coder view: ${e.message}`);
        }
      }
      if (!eng) return;
      const init = {};
      if (eng.em) init.em = { action: "accept", final: eng.em.code, reason: null };
      (eng.dx || []).forEach((r) => { init[r.id] = { action: "accept", final: r.code, reason: null }; });
      setDec(init);
      setWithheld(Object.fromEntries((eng.withheld || []).map((w) => [w.id, { bill: false, reason: null }])));
      setEngine(eng);
    })();
  }, [encounterId, live]);

  useEffect(() => { const t = setInterval(() => setSeconds((s) => s + 1), 1000); return () => clearInterval(t); }, []);

  if (!engine) {
    // UI SEAM (P2): never turn an expired/denied session into a mock claim.
    if (error) return <p>{error}</p>;
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm">
        <RefreshCw size={16} className="animate-spin" /> Pre-coding encounter {encounterId}…
      </div>
    );
  }

  // UI SEAM (2026-08-17): the engine reports `degraded` when a run wasn't a real
  // coding attempt — today that means the offline demo model doesn't cover this
  // encounter. Without this the reviewer sees an empty claim and concludes the
  // engine is broken. Deliberately unstyled beyond the minimum; restyle freely,
  // but keep the reason visible whenever `engine.degraded` is set.
  if (engine.degraded) {
    return (
      <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 shadow-sm">
        <p className="text-sm font-medium text-amber-900">No codes proposed for this encounter</p>
        <p className="mt-1 text-sm leading-relaxed text-amber-800">{engine.degraded.detail}</p>
        <p className="mt-2 text-xs text-amber-700">
          Encounter {engine.encounterId} · {engine.inferenceMode} mode
          {engine.inferenceModel ? ` (${engine.inferenceModel})` : ""}
        </p>
      </div>
    );
  }

  const set = (key, val) => setDec((d) => ({ ...d, [key]: val }));
  const captured = [
    ...(engine.em ? [{ k: "EM", id: "em", row: engine.em }] : []),
    ...(engine.dx || []).map((r) => ({ k: "ICD10", id: r.id, row: r })),
  ];
  const agreed = captured.filter(({ id }) => dec[id]?.action === "accept").length;

  const submit = async () => {
    if (pendingPayload.current) {
      try {
        const r = await api("/api/feedback", { method: "POST", body: pendingPayload.current });
        setDone({ ...pendingPayload.current, _replayed: r.replayed });
      } catch (e) {
        setError(`feedback submit failed: ${e.message}`);
      }
      return;
    }
    const decisions = [];
    if (engine.em) decisions.push({ kind: "EM", engine_code: engine.em.code, action: dec.em.action, final_code: dec.em.final, reason_code: dec.em.reason, note: "" });
    (engine.dx || []).forEach((r) => { const d = dec[r.id]; decisions.push({ kind: "ICD10", engine_code: r.code, action: d.action, final_code: d.final, reason_code: d.reason, note: "" }); });
    (engine.withheld || []).forEach((w) => {
      const s = withheld[w.id];
      if (s?.bill) decisions.push({ kind: "ICD10", engine_code: null, action: "add", final_code: w.code, reason_code: s.reason || "dx_missed_by_engine", note: `coder billed engine-${w.status} code` });
      else decisions.push({ kind: "ICD10", engine_code: w.code, action: "reject", final_code: null, reason_code: "other", note: `confirmed not billed (engine ${w.status})` });
    });
    added.forEach((a) => { if (a.code) decisions.push({ kind: "ICD10", engine_code: null, action: "add", final_code: a.code, reason_code: "dx_missed_by_engine", note: a.note || "" }); });
    const payload = {
      review_id: reviewId.current, encounter_id: engine.encounterId,
      review_context: engine.reviewContext,
      encounter_version: engine.encounterVersion || 1,
      submitted_em: engine.submittedEm, engine_em: engine.em?.code || null, final_em: dec.em?.final || null,
      decisions, time_spent_seconds: seconds, overall_note: "",
    };
    if (live) {
      // Freeze the exact first payload. A response-lost retry must reuse both
      // the UUID and timer value or the server correctly treats it as a conflict.
      pendingPayload.current = payload;
      try {
        const r = await api("/api/feedback", { method: "POST", body: payload });
        setDone({ ...payload, _replayed: r.replayed });
      } catch (e) {
        setError(`feedback submit failed: ${e.message}`);
      }
    } else {
      setDone(payload);
    }
  };

  return (
    <div className="mx-auto max-w-3xl">
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-slate-900">Suggested claim · {engine.encounterId}</p>
            <p className="text-xs text-slate-500">You're in control — the engine pre-coded this from the documentation. Review, correct, submit.</p>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">
              {engine.submittedEm && <>submitted <code className="font-mono">{engine.submittedEm}</code> · </>}
              engine <code className="font-mono">{engine.em?.code || "—"}</code>
            </span>
            <Pill tone="bg-slate-100 text-slate-600 ring-slate-300"><Clock size={12} className="mr-1" />{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")}</Pill>
          </div>
        </div>

        {error && <p className="mt-3 rounded-lg bg-red-50 p-2.5 text-sm text-red-700">{error}</p>}

        {engine.em && (
          <>
            <p className="mt-4 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">E/M level</p>
            <CodeRow row={engine.em} kind="EM" decision={dec.em} set={(v) => set("em", v)} />
          </>
        )}

        <p className="mt-4 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Diagnoses</p>
        {(engine.dx || []).length === 0 && <p className="text-sm text-slate-400">no diagnoses proposed</p>}
        {(engine.dx || []).map((r) => <CodeRow key={r.id} row={r} kind="ICD10" decision={dec[r.id]} set={(v) => set(r.id, v)} />)}

        {(engine.withheld || []).length > 0 && (
          <>
            <p className="mt-5 mb-1 text-xs font-medium uppercase tracking-wide text-slate-400">Engine withheld these — your call</p>
            {(engine.withheld || []).map((w) => (
              <div key={w.id} className="border-t border-slate-100 py-3">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="w-16 font-mono text-sm text-slate-900">{w.code}</code>
                  <span className="flex-1 text-sm text-slate-700">{w.label}</span>
                  <Pill tone={w.status === "held" ? "bg-amber-50 text-amber-700 ring-amber-200" : "bg-red-50 text-red-700 ring-red-200"}>{w.status}</Pill>
                  <div className="flex items-center gap-1 rounded-lg bg-slate-50 p-0.5">
                    <button onClick={() => setWithheld((s) => ({ ...s, [w.id]: { ...s[w.id], bill: false } }))} className={seg(!withheld[w.id]?.bill, "bg-slate-200 text-slate-700")}>Don't bill</button>
                    <button onClick={() => setWithheld((s) => ({ ...s, [w.id]: { ...s[w.id], bill: true } }))} className={seg(withheld[w.id]?.bill, "bg-emerald-100 text-emerald-700")}>Bill it</button>
                  </div>
                </div>
                <Why text={w.why} evidence={w.evidence} history={w.history} />
              </div>
            ))}
          </>
        )}

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
          <div className="flex items-center gap-2 text-sm font-medium text-emerald-800">
            <Check size={18} />
            Feedback recorded{done._stored ? ` (${done._stored} in store)` : ""} — final E/M {done.final_em}, {done.decisions.length} decisions, {done.time_spent_seconds}s.
          </div>
          <p className="mt-2 text-xs text-slate-500">The tuning pipeline turns this into a gold row and a ranked rule/prompt backlog (see analyze_feedback.py).</p>
          <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-3 text-xs leading-relaxed text-slate-100">{JSON.stringify(done, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
