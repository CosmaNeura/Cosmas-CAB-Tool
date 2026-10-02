import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { readAthenaEnvelope, athenaSourceReadFields, athenaConnectionMessage, mergeAthenaBilling, athenaBillingReadBody, athenaField, athenaDepartments, athenaPhoneReview, athenaSourceDepartment, athenaImportMessage, fieldDisplay, athenaFailure, ambiguousAthenaFailure } from "./athenaCaseReview.js";

const button = "rounded-lg border border-slate-300 px-3 py-2 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50";
const primary = "rounded-lg bg-sky-800 px-3 py-2 text-sm font-medium text-white hover:bg-sky-900 disabled:opacity-50";
const input = "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 disabled:bg-slate-50";
const labels = { first_name: "First name", last_name: "Last name", birth_date: "Birth date",
  member_id: "Member ID", insured_id: "Insured policy ID", group_number: "Group number", sequence: "Policy sequence",
  plan_name: "Plan", subscriber_display_name: "Subscriber", subscriber_first_name: "Subscriber first name",
  subscriber_last_name: "Subscriber last name", relationship: "Relationship to insured", policy_expiration_date: "Policy expiration",
  eligibility_status: "Source eligibility status", eligibility_checked_at: "Source eligibility check time" };
function Fields({ fields, names }) {
  return <dl className="grid gap-3 sm:grid-cols-2">{names.map((name) => <div key={name} className="min-w-0">
    <dt className="text-[11px] text-slate-500">{labels[name] || name.replaceAll("_", " ")}</dt>
    <dd className="break-words text-sm text-slate-800">{fieldDisplay(athenaField(fields, name))}</dd>
  </div>)}</dl>;
}
function Check({ label, checked, onChange, disabled }) {
  return <label className="flex items-start gap-2 text-xs leading-relaxed text-slate-700"><input type="checkbox" className="mt-0.5" checked={checked} onChange={(e) => onChange(e.target.checked)} disabled={disabled} />{label}</label>;
}

export default function AthenaCaseReview(props) {
  return <CaseBody key={props.task.chaseId} {...props} />;
}
function CaseBody({ task, active, manageChase, blocked, onOpenEncounter, onApplied, onReviewLock }) {
  const chaseId = task.chaseId;
  const billingAvailable = task.athenaReview?.billingAvailable === true;
  const [view, setView] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [retry, setRetry] = useState(null), [stale, setStale] = useState(false), [notice, setNotice] = useState("");
  const [connection, setConnection] = useState(null);
  const [patient, setPatient] = useState(""), [phone, setPhone] = useState(""), [email, setEmail] = useState(false);
  const [phoneCountry, setPhoneCountry] = useState("");
  const [policies, setPolicies] = useState([]), [match, setMatch] = useState(false), [insurance, setInsurance] = useState(false);
  const [reason, setReason] = useState(""), [department, setDepartment] = useState(""), [encounter, setEncounter] = useState("");
  const [billing, setBilling] = useState(null), [finished, setFinished] = useState(false), [clinical, setClinical] = useState(false);
  const [billingStale, setBillingStale] = useState(false);
  const [context, setContext] = useState({ patientAge: "", patientSex: "", encounterType: "", placeOfService: "", providerSpecialty: "" });
  const request = useRef({ sequence: 0, busy: false, controller: null });
  const baseLocked = busy || blocked || !manageChase || !!retry || stale;
  const sourceLocked = baseLocked || !!connection && !connection.readAllowed;
  const locked = sourceLocked || !!connection && !connection.currentReviewMatchesBinding;
  const billingLocked = locked || billingStale;
  const candidate = view?.candidate;
  const currentPatient = candidate && (connection ? connection.currentReviewMatchesBinding : patient === candidate.source.patientId);
  const appliedCurrent = view?.applied && view.applied.reviewId === view.reviewId && (!connection || connection.currentReviewMatchesBinding);
  const departments = athenaDepartments(candidate?.patient?.fields);
  const phoneReview = athenaPhoneReview(candidate?.contact.candidates.find((c) => c.field?.name === phone), phoneCountry);
  function clearSelections() { setPhone(""); setPhoneCountry(""); setEmail(false); setPolicies([]); setMatch(false); setInsurance(false); setReason(""); setDepartment(""); setBilling(null); setBillingStale(false); setEncounter(""); setFinished(false); setClinical(false); setContext({ patientAge: "", patientSex: "", encounterType: "", placeOfService: "", providerSpecialty: "" }); }
  function selectDepartment(value) {
    setDepartment(value); setBilling(null); setBillingStale(false); setEncounter(""); setFinished(false); setClinical(false);
    setContext({ patientAge: "", patientSex: "", encounterType: "", placeOfService: "", providerSpecialty: "" });
  }

  useEffect(() => () => { request.current.sequence++; request.current.controller?.abort(); }, []);
  useEffect(() => {
    onReviewLock?.(busy || !!retry);
    return () => onReviewLock?.(false);
  }, [busy, retry, onReviewLock]);
  async function run(command = null) {
    if (request.current.busy || blocked) return;
    const sequence = ++request.current.sequence, controller = new AbortController();
    let timedOut = false, deadline;
    const timeout = new Promise((_, reject) => {
      deadline = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
        reject(new Error("Athena request timed out."));
      }, 30_000);
    });
    request.current = { sequence, busy: true, controller };
    setBusy(true); setError(""); setNotice("");
    let receipt = null;
    try {
      const data = await Promise.race([api(command?.path || "/api/athena/card-chase", { method: "POST",
        body: command?.body || { chaseId }, signal: controller.signal }), timeout]);
      if (timedOut) throw new Error("Athena request timed out.");
      if (sequence !== request.current.sequence) return;
      if (command?.kind === "billing") {
        const next = mergeAthenaBilling(billing, data, view, command.body.departmentId, {
          encounter: command.body.sourceEncounterId, continuation: command.continuation,
          discoveryToken: command.body.discoveryToken ?? null,
        });
        const sameWindow = command.continuation && next.pagination.windowStart === billing.pagination.windowStart;
        setBilling(next); setBillingStale(false);
        if (!sameWindow) {
          if (!command.body.sourceEncounterId) setEncounter("");
          setFinished(false); setClinical(false);
          setContext({ patientAge: "", patientSex: "", encounterType: "", placeOfService: "", providerSpecialty: "" });
        }
      } else {
        const parsed = readAthenaEnvelope(data, chaseId, command, connection !== null);
        const next = parsed.view; receipt = parsed.receipt; setConnection(parsed.connection);
        setView(next); setPatient(next.candidate?.source.patientId || ""); clearSelections();
      }
      setRetry(null); setStale(false);
      if (command) setNotice(receipt?.replayed && !receipt.current
        ? "The earlier request was recovered. The current case review is shown; review it before starting another action." : command.notice);
      if (onApplied && ["/api/athena/card-chase/apply", "/api/athena/billing/apply"].includes(command?.path)) {
        // The mutation is confirmed. A host refresh failure must never turn it
        // into an ambiguous save or repeat the patient/clinical confirmation.
        Promise.resolve().then(() => {
          if (sequence === request.current.sequence) return onApplied(chaseId);
        }).catch(() => {
          if (sequence === request.current.sequence) setNotice(`${command.notice} Other case panels could not be refreshed. Refresh the case to load the saved information.`);
        });
      }
    } catch (failure) {
      if (sequence !== request.current.sequence || (failure.name === "AbortError" && !timedOut)) return;
      setError(athenaFailure(failure, { kind: command?.kind || "snapshot", timedOut, continuation: command?.continuation }));
      if (command && command.kind !== "billing") {
        const ambiguous = timedOut || ambiguousAthenaFailure(failure);
        setRetry(ambiguous ? command : null); setStale(!ambiguous && [403, 404, 409].includes(failure.status));
      }
      if (command?.kind === "billing") {
        // Keep the validated window and selected note intact on failed reads.
        if (failure.status === 409) setBillingStale(true);
        if ([403, 404].includes(failure.status)) setStale(true);
      }
      if (!command) { setView(null); setBilling(null); }
    } finally {
      window.clearTimeout(deadline);
      if (sequence === request.current.sequence) { request.current.busy = false; setBusy(false); }
    }
  }
  useEffect(() => { if (active && !view) run(); }, [active]);
  function change(path, body, message) {
    if ((path === "/api/athena/card-chase/read" ? sourceLocked : locked) || !view) return;
    run({ path, kind: "change", body: { chaseId, operationId: crypto.randomUUID(), expectedRevision: view.revision, ...body }, notice: message });
  }
  function readBilling(selected = null, continuation = false) {
    if (locked || !appliedCurrent || !currentPatient || !department || (billingStale && (selected || continuation))) return;
    run({ kind: "billing", continuation, path: "/api/athena/billing/read",
      body: athenaBillingReadBody(chaseId, department, billing, { encounter: selected, continuation }),
      notice: selected ? "Read the complete source text before importing." : continuation
        ? "Encounter results updated. Select the encounter you intend to review."
        : "Select the encounter you intend to review." });
  }
  function applySource(event) {
    event.preventDefault();
    if (!currentPatient || !match || !reason.trim() || (policies.length > 0 && !insurance)
      || (phone && !phoneReview.normalizedValue)) return;
    change("/api/athena/card-chase/apply", { reviewId: view.reviewId, confirmedPatientMatch: match,
      selectedPhoneField: phone || null, selectedEmail: email, selectedPolicyIds: policies,
      selectedPhoneCountry: phoneReview.needsCountry ? phoneCountry : null,
      confirmedPolicyReview: insurance, reviewReason: reason }, "Selected source observations applied. Consent remains unknown; case state is unchanged.");
  }
  function importNote(event) {
    event.preventDefault();
    if (billingLocked || !billing?.note || !finished || !clinical || !Object.values(context).every((v) => String(v).trim()) || !reason.trim()) return;
    change("/api/athena/billing/apply", { departmentId: department, sourceEncounterId: encounter,
      discoveryToken: billing.pagination?.selectionToken ?? null,
      expectedNoteSha256: billing.note.responseSha256, expectedMetadataSha256: billing.note.metadataSha256,
      confirmedPatientMatch: true, confirmedFinishedNote: finished, confirmedClinicalContext: clinical,
      ...context, patientAge: Number(context.patientAge), reviewReason: reason }, "Reviewed source imported into CAB.");
  }
  const editContext = (key, value) => { setContext((c) => ({ ...c, [key]: value })); setClinical(false); };

  return <section aria-label="Athena case review" aria-busy={busy} className="space-y-4 py-4">
    <div className="flex items-start justify-between gap-3"><p className="text-xs text-slate-500">{connection?.binding ? `Athena · ${connection.binding.environment === "preview" ? "Preview" : "Production"} connection` : view?.mode === "preview" ? "Athena Preview · synthetic cohort" : "Athena source review"} · Staff-confirmed read/import</p>
      <button className={button} disabled={busy || blocked || !!retry} onClick={() => run()}>Refresh case review</button></div>
    {view?.mode === "mock" && <p className="rounded-lg bg-sky-50 p-3 text-xs text-sky-900">Local mock · Invented patient and insurance responses. No Athena connection is used.</p>}
    {error && <div role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-900"><p>{error}</p>{retry && <button className={`${button} mt-2`} disabled={busy || blocked} onClick={() => run(retry)}>Retry same request</button>}</div>}
    {notice && <p role="status" className="text-xs text-sky-800">{notice}</p>}
    {!view ? <p className="text-sm text-slate-500">{busy ? "Loading source review…" : "Source review unavailable."}</p> : <>
      {view.applied && <details open className="rounded-xl border border-emerald-200 p-4"><summary className="cursor-pointer text-sm font-semibold text-slate-900">Applied case information</summary>
        <p className="my-2 text-xs text-slate-500">Applied {new Date(view.applied.at).toLocaleString()} · Consent unknown{!appliedCurrent && " · Historical source observation"}</p>
        {view.applied.sourceBinding?.environment === "production" && <p className="mb-2 text-xs text-slate-600">Athena Production · Operational source.</p>}
        <Fields fields={view.applied.patient.fields} names={["first_name", "last_name", "birth_date"]} />
        <ul className="mt-3 space-y-1 text-sm">{view.applied.contacts.map((c) => <li key={c.field} className="break-words">{c.field === "email" ? "Email" : "Phone"}: {c.value}{c.normalization && <p className="text-xs text-slate-500">Source: {c.normalization.sourceValue} · United States country confirmed by staff.</p>}</li>)}</ul>
        {view.applied.policies.map((policy) => <details key={policy.candidateId} className="mt-3 rounded-lg border border-emerald-100 p-2 text-xs"><summary className="cursor-pointer font-medium">Applied insurance · {fieldDisplay(athenaField(policy.fields,"plan_name"))}</summary><div className="mt-2"><Fields fields={policy.fields} names={["sequence","member_id","insured_id","group_number","subscriber_first_name","subscriber_last_name","relationship"]} /></div></details>)}
        <p className="mt-2 text-xs text-slate-500">{view.applied.policies.length} insurance observation{view.applied.policies.length === 1 ? "" : "s"} selected. This does not verify coverage or complete the case.</p>
      </details>}
      {connection ? <div className="rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold">Mapped Athena source</h3>
        <p role="status" className={`mt-2 text-xs ${connection.readAllowed && !view.redacted ? "text-slate-600" : "text-amber-800"}`}>{athenaConnectionMessage(connection, view.redacted)}</p>
        {connection.binding && <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-2">
          <div><dt className="text-slate-500">Athena environment / practice</dt><dd className="mt-1 font-medium">{connection.binding.environment === "preview" ? "Preview" : "Production"} · {connection.binding.vendorPracticeId}</dd></div>
          <div><dt className="text-slate-500">Mapped source patient ID</dt><dd className="mt-1 font-medium">{connection.binding.sourcePatientId}</dd></div>
        </dl>}
        {connection.binding?.environment === "production" && <p className="mt-3 text-xs text-slate-600">Coding uses a separately approved model service. Availability is checked when you open the imported note.</p>}
        {connection.binding && <details className="mt-3 text-xs text-slate-500"><summary className="cursor-pointer">Mapping details</summary><p className="mt-2">Connection revision {connection.binding.connectionRevision} · Patient mapping revision {connection.binding.bindingRevision}</p></details>}
        <button className={`${primary} mt-3`} disabled={sourceLocked} onClick={() => change("/api/athena/card-chase/read", athenaSourceReadFields(connection, patient), "Review the mapped source identity and exact fields before applying.")}>Read mapped source</button>
        {!connection.readAllowed && <p className="mt-2 text-xs text-slate-500">Refresh case review after the connection or mapping is updated.</p>}
      </div> : <div className="rounded-xl border border-slate-200 p-4"><h3 className="text-sm font-semibold">Read selected Athena patient</h3>
        <p className="mt-1 text-xs text-slate-500">Choose the source patient explicitly, then compare identity with this case before applying.</p>
        <div className="mt-3 flex items-end gap-3"><label className="min-w-0 flex-1 text-xs">Synthetic Athena patient<select aria-label="Synthetic Athena patient" className={input} value={patient} disabled={locked} onChange={(e) => {setPatient(e.target.value); clearSelections();}}><option value="">Select patient</option>{Array.from({length:7},(_,i) => String(60178+i)).map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
          <button className={primary} disabled={locked || !patient} onClick={() => change("/api/athena/card-chase/read", athenaSourceReadFields(null, patient), "Review the source identity and exact fields before applying.")}>Read source</button></div>
      </div>}
      {candidate && <form onSubmit={applySource} className="space-y-4 rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold">Source fields requiring review</h3><p className="text-xs text-slate-500">Patient read: {candidate.reads.patient.state} · Insurance read: {candidate.reads.insurance.state} · {new Date(candidate.source.observedAt).toLocaleString()}</p>
        {view.mode === "production" && <p className="text-xs text-slate-600">Athena Production · Operational source. Review the observations before applying.</p>}
        {!currentPatient && <p className="text-xs text-amber-800">{connection ? "This source snapshot is not current for the saved mapping or source approval. Read the mapped source again before applying." : "The selected patient differs from this snapshot. Read the selected source first."}</p>}
        <Fields fields={candidate.patient.fields} names={["first_name", "last_name", "birth_date"]} />
        <fieldset disabled={locked || !currentPatient} className="space-y-3">
          <Check checked={match} onChange={setMatch} label="I compared the source identity with this case and confirm the patient link." />
          <label className="block text-xs">Phone to apply<select aria-label="Phone to apply" className={input} value={phone} onChange={(e) => { setPhone(e.target.value); setPhoneCountry(""); }}><option value="">Do not apply a phone</option>{candidate.contact.candidates.filter((c) => c.kind !== "email" && c.field).map((c) => <option key={c.kind} value={c.field.name} disabled={!athenaPhoneReview(c).selectable}>{c.kind === "mobile" ? "Mobile" : "Home"} · {fieldDisplay(c.field)} ({c.formatStatus === "country_confirmation_required" ? "confirm country" : c.formatStatus})</option>)}</select></label>
          {phoneReview.needsCountry && <div className="space-y-2 rounded-lg border border-amber-200 p-3">
            <label className="block text-xs">Confirmed phone country<select aria-label="Confirmed phone country" className={input} value={phoneCountry} onChange={(e) => setPhoneCountry(e.target.value)}><option value="">Confirm the country before applying</option><option value="US">United States (+1)</option></select></label>
            <p className="text-xs text-slate-600">Select United States only after checking the number’s country. CAB does not infer it from the digits.</p>
            {phoneReview.normalizedValue && <p className="text-sm font-medium text-slate-800">Number to apply: {phoneReview.normalizedValue}</p>}
          </div>}
          {candidate.contact.candidates.filter((c) => c.kind === "email" && c.field).map((c) => <Check key="email" checked={email} onChange={setEmail} disabled={c.formatStatus !== "plausible"} label={`Apply email: ${fieldDisplay(c.field)}`} />)}
          <p className="text-xs text-slate-500">Phone ownership and SMS, call, and email consent remain unknown. Applying fields does not authorize contact.</p>
          <div className="space-y-3">{candidate.insurance.policies.map((p) => <div key={p.candidateId} className="rounded-lg bg-slate-50 p-3">
            <Check checked={policies.includes(p.candidateId)} onChange={(checked) => {setPolicies((old) => checked ? [...old,p.candidateId] : old.filter((id) => id !== p.candidateId)); setInsurance(false);}} label={`Select insurance observation ${fieldDisplay(athenaField(p.fields,"plan_name"))}`} />
            <div className="mt-3"><Fields fields={p.fields} names={["sequence","member_id","insured_id","group_number","subscriber_display_name","subscriber_first_name","subscriber_last_name","relationship","policy_expiration_date","eligibility_status","eligibility_checked_at"]} /></div>
            {p.issues.length > 0 && <p className="mt-2 text-xs text-amber-800">Needs review: {p.issues.map((s) => s.replaceAll("_"," ")).join(" · ")}</p>}
          </div>)}</div>
          {policies.length > 0 && <Check checked={insurance} onChange={setInsurance} label="I reviewed the selected insurance observations, including conflicting or missing values. They remain source observations, not verified coverage." />}
          <label className="block text-xs">Application review reason<textarea aria-label="Application review reason" className={input} maxLength={600} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button className={primary} disabled={!match || !reason.trim() || (phone && !phoneReview.normalizedValue) || (policies.length > 0 && !insurance) || candidate.reads.patient.state !== "complete"}>Apply selected fields</button>
        </fieldset>
      </form>}
      {appliedCurrent && billingAvailable && <section className="space-y-3 rounded-xl border border-slate-200 p-4" aria-label="Athena billing note import"><h3 className="text-sm font-semibold">Review a finished note for CAB</h3>
        <p className="text-xs text-slate-500">This reads encounter documentation. Staff must confirm its patient link, complete note, and clinical context before importing.</p>
        {departments.length > 0 && <div className="space-y-2"><p className="text-xs text-slate-500">Departments reported by this patient record. Choose a verified chart query context, or enter another verified department below. Returned encounters may belong to other departments.</p>
          <div className="flex flex-wrap gap-2">{departments.map((choice) => <button key={choice.id} type="button" className={button} disabled={locked} aria-pressed={department === choice.id} onClick={() => selectDepartment(choice.id)}>{choice.label}</button>)}</div>
        </div>}
        <label className="block text-xs">Chart query department ID<input aria-label="Chart query department ID" className={input} inputMode="numeric" maxLength={18} value={department} disabled={locked} onChange={(e) => selectDepartment(e.target.value)} /></label>
        <button className={button} disabled={locked || !currentPatient || !/^[1-9][0-9]{0,17}$/.test(department)} onClick={() => readBilling()}>Find encounters</button>
        {billingStale && <p role="status" className="text-xs text-amber-800">Previous results remain visible. Select Find encounters before loading more, previewing, or importing.</p>}
        {billing && <><p role="status" className="text-xs text-slate-500">Encounter read: {billing.state} · {billing.encounters.length} encounter{billing.encounters.length === 1 ? "" : "s"} loaded{billing.pagination && " in this window"}{billing.pagination?.windowStart > 0 && ` · Source results start at ${billing.pagination.windowStart + 1}`}</p>
          {billing.pagination?.nextToken ? <div className="space-y-2">
            <p className="text-xs text-slate-500">{billing.pagination.nextStartsWindow
              ? "This window has reached its bounded size. Load older encounters to replace the list with the next window and clear the selected note."
              : "More source encounters are available. Each click reads up to ten additional encounters."}</p>
            <button className={button} disabled={billingLocked} onClick={() => readBilling(null, true)}>{billing.pagination.nextStartsWindow ? "Load older encounters" : "Load more encounters"}</button>
          </div> : <p className="text-xs text-slate-500">{billing.state === "complete" || billing.state === "empty"
            ? "The source reported no more encounters at the last read."
            : "This is a partial or unavailable source list. Continuation is unavailable; Find encounters starts a new search."}</p>}
          <label className="block text-xs">Source encounter<select aria-label="Source encounter" className={input} value={encounter} disabled={billingLocked} onChange={(e) => {setEncounter(e.target.value); setBilling({...billing,note:null}); setFinished(false); setClinical(false);}}><option value="">Select encounter</option>{billing.encounters.map((row) => <option key={row.encounterid.value} value={row.encounterid.value}>{row.encounterid.value} · {row.encounterdate?.value || "Date not supplied"} · {row.status?.value || "Status not supplied"} · Encounter department {athenaSourceDepartment(row.departmentid?.value)}</option>)}</select></label>
          <button className={button} disabled={billingLocked || !encounter} onClick={() => readBilling(encounter)}>Preview source note</button></>}
        {billing?.note && <form onSubmit={importNote} className="space-y-3 border-t border-slate-200 pt-3">
          <p className="text-xs text-slate-600">Chart query department {billing.departmentId} · Encounter department {athenaSourceDepartment(billing.note.metadata?.departmentid?.value)}{billing.note.sourceDepartmentOrigin === "metadata" ? " (from encounter metadata; not supplied in list)" : ""}</p>
          <pre aria-label="Source note text" className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-50 p-3 font-sans text-sm leading-relaxed">{billing.note.plainText || "No note text supplied."}</pre>
          <p className="text-xs text-slate-500">Derived source text · Source signing and atomicity are unverified. Read the complete note and confirm against the source record.</p>
          <fieldset disabled={billingLocked} className="space-y-3">
            <Check checked={finished} onChange={setFinished} label="I confirm this is the complete finished note for the patient linked to this case." />
            <div className="grid gap-3 sm:grid-cols-2"><label className="text-xs">Patient age<input aria-label="Patient age" type="number" min="0" max="130" className={input} value={context.patientAge} onChange={(e) => editContext("patientAge",e.target.value)} /></label>
              {[["patientSex","Patient sex",["M","F","other","unknown"]],["encounterType","Encounter type",["office_new","office_established","telehealth_new","telehealth_established"]],["placeOfService","Recorded POS",["11","02","10"]]].map(([key,label,choices]) => <label className="text-xs" key={key}>{label}<select aria-label={label} className={input} value={context[key]} onChange={(e) => editContext(key,e.target.value)}><option value="">Select documented value</option>{choices.map((choice) => <option key={choice} value={choice}>{choice.replaceAll("_"," ")}</option>)}</select></label>)}
              <label className="text-xs sm:col-span-2">Provider specialty<input aria-label="Provider specialty" className={input} maxLength={100} value={context.providerSpecialty} onChange={(e) => editContext("providerSpecialty",e.target.value)} /></label></div>
            <Check checked={clinical} onChange={setClinical} label="I verified these clinical context values from the source. Appointment duration is not eligible E/M time." />
            <label className="block text-xs">Import review reason<textarea aria-label="Import review reason" className={input} maxLength={600} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            <button className={primary} disabled={!finished || !clinical || !reason.trim() || !billing.note.plainText.trim() || !Object.values(context).every((v) => String(v).trim())}>Import reviewed note into CAB</button>
          </fieldset>
        </form>}
      </section>}
      {view.billingImport && <div className="rounded-lg bg-emerald-50 p-3 text-sm text-emerald-950"><p>{athenaImportMessage(view.billingImport)}</p>{view.billingImport.source === "athena_staff_review_operational" && <p className="mt-1 text-xs">Athena Production · Operational source.</p>}<p className="mt-1 text-xs">{view.billingImport.encounterId}</p><p className="mt-1 text-xs">Chart query department {view.billingImport.departmentId} · Encounter department {athenaSourceDepartment(view.billingImport.sourceDepartmentId)}{view.billingImport.sourceDepartmentOrigin === "metadata" ? " (from encounter metadata)" : ""}</p>
        {onOpenEncounter && <button className={`${button} mt-2`} disabled={locked} onClick={() => onOpenEncounter(view.billingImport.encounterId)}>Open CAB encounter</button>}</div>}
    </>}
  </section>;
}
