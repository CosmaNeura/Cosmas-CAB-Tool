import React, { useEffect, useRef, useState } from "react";
import {
  CheckCircle2, CreditCard, LockKeyhole, Phone,
  RefreshCw, Search, UserCheck,
} from "lucide-react";
import { api } from "./api.js";
import CaseCommunications from "./CaseCommunications.jsx";
import { reviewCounts } from "./communicationsEventReview.js";
import UnassignedTexts from "./UnassignedTexts.jsx";
import {hasCommunicationCounts, listedCaseIds, mergeListedCounts, pollVisibleCounts, readListedCounts} from "./communicationCounts.js";
import {
  CHASE_CHANNELS, chaseAttemptTime, chaseMinutesLabel, chaseOutcomeLabel, readChaseActivity,
} from "./chaseActivity.js";
import {
  seedConfirmationIsSafe,
  syntheticSeedConfirmationMessage,
  syntheticSeedErrorMessage,
} from "./syntheticDemoSeed.js";


const FILTERS = [
  ["", "All"],
  ["missing", "Missing card"],
  ["expired", "Expired"],
  ["eligibility", "Eligibility"],
];

const STATUS = {
  detected: ["Detected", "bg-sky-50 text-sky-700 ring-sky-200"],
  needs_review: ["Human review", "bg-amber-50 text-amber-800 ring-amber-200"],
  ready: ["Ready", "bg-emerald-50 text-emerald-700 ring-emerald-200"],
  in_progress: ["In progress", "bg-violet-50 text-violet-700 ring-violet-200"],
  submitted: ["Submitted", "bg-slate-100 text-slate-700 ring-slate-300"],
  verified_closed: ["Verified & closed", "bg-emerald-50 text-emerald-700 ring-emerald-200"],
};

function Pill({ tone, children }) {
  return (
    <span className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      {children}
    </span>
  );
}

const OUTCOMES = [
  ["reached", "Reached patient"],
  ["left_message", "Left message"],
  ["no_answer", "No answer"],
  ["declined", "Patient declined"],
];

function CaseActivity({ value }) {
  const activity = readChaseActivity(value);
  return (
    <section aria-label="Selected case activity" className="mt-4 rounded-xl border border-slate-200 p-3">
      <h3 className="text-sm font-semibold text-slate-900">Contact activity</h3>
      {!activity ? (
        <p className="mt-2 text-sm text-slate-500">Activity unavailable. This case’s contact history could not be verified.</p>
      ) : (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-3">
            <div><dt className="text-xs text-slate-500">Recorded contact attempts</dt><dd className="text-lg font-semibold text-slate-900">{activity.totalAttempts}</dd></div>
            <div><dt className="text-xs text-slate-500">Staff minutes</dt><dd className="text-lg font-semibold text-slate-900">{chaseMinutesLabel(activity.staffMinutes)}</dd></div>
          </dl>
          <dl className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
            {Object.entries(CHASE_CHANNELS).map(([channel, label]) => (
              <div key={channel} className="flex gap-1"><dt>{label}:</dt><dd>{activity.countsByChannel[channel]}</dd></div>
            ))}
          </dl>
          {activity.lastAttempt ? (
            <div className="mt-3 border-t border-slate-100 pt-3">
              <p className="text-xs font-medium text-slate-500">Last recorded contact</p>
              <p className="mt-1 text-sm text-slate-800"><time dateTime={activity.lastAttempt.at}>{chaseAttemptTime(activity.lastAttempt.at)}</time></p>
              <p className="mt-1 text-xs text-slate-600">{CHASE_CHANNELS[activity.lastAttempt.channel]} · {chaseOutcomeLabel(activity.lastAttempt.outcome)}</p>
            </div>
          ) : <p className="mt-3 text-sm text-slate-600">No recorded contact attempts.</p>}
          <p className="mt-3 text-xs leading-relaxed text-slate-500">
            Counts cover recorded contacts; queued messages and failed delivery events are not included. Sent or accepted does not confirm delivery or that the patient read it.
          </p>
          {activity.history.length > 0 && (
            <details className="mt-3 border-t border-slate-200 pt-3">
              <summary className="cursor-pointer text-sm font-medium text-slate-700">Contact history ({activity.history.length}{activity.truncated ? ` of ${activity.totalAttempts}` : ""})</summary>
              {activity.truncated && <p className="mt-2 text-xs text-slate-500">Showing the latest 50 recorded contacts. Counts and staff minutes include the full history.</p>}
              <ol aria-label="Contact history" className="mt-3 space-y-3">
                {activity.history.map((entry, index) => (
                  <li key={`${entry.at}-${index}`} className="rounded-lg bg-slate-50 p-3 text-xs text-slate-600">
                    <time dateTime={entry.at} className="font-medium text-slate-800">{chaseAttemptTime(entry.at)}</time>
                    <p className="mt-1">{CHASE_CHANNELS[entry.channel]} · {entry.performer === "staff" ? "Staff" : "System"} · {chaseOutcomeLabel(entry.outcome)}</p>
                    <p className="mt-1">{chaseMinutesLabel(entry.minutes)} staff min</p>
                  </li>
                ))}
              </ol>
            </details>
          )}
        </>
      )}
    </section>
  );
}

function FollowUpTask({
  task, loading, error, manageChase, actionBusy, actionError,
  actionNotice, retryAction, onAction, onReviewCounts, onReviewLock,
}) {
  const [outcome, setOutcome] = useState("reached");
  const [minutes, setMinutes] = useState("0");
  const [note, setNote] = useState("");
  const [confirming, setConfirming] = useState(null);
  const [panel, setPanel] = useState("summary");
  const hasCommunications = task?.communications?.available === true && ["simulated", "durable"].includes(task.communications.mode);
  const panels = ["summary", "activity", ...(hasCommunications ? ["communications"] : [])];
  const may = (action) => (
    manageChase && Array.isArray(task?.allowedActions)
      && task.allowedActions.includes(action)
  );

  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white p-5 text-sm text-slate-500 shadow-sm">
        <RefreshCw size={16} className="animate-spin" /> Loading the audited contact task…
      </div>
    );
  }
  if (error) {
    return <p className="rounded-xl bg-red-50 p-3 text-sm text-red-700">{error}</p>;
  }
  if (!task) {
    return (
      <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-5 text-sm text-slate-500">
        Select a case to see contact details and the next action.
      </div>
    );
  }
  return (
    <section aria-label="Selected case workspace" className="min-w-0 rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-start justify-between gap-3 px-5 pt-5">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-slate-400">Case workspace</p>
          <h2 className="mt-1 text-xl font-semibold tracking-tight text-slate-900">{task.patientLabel}</h2>
          <p className="mt-1 text-xs text-slate-500">{task.assigned ? "Assigned" : "Unassigned"} · Insurance-card follow-up</p>
        </div>
        <Pill tone={(STATUS[task.state] || ["", "bg-slate-100 text-slate-600 ring-slate-300"])[1]}>
          {(STATUS[task.state] || [task.state])[0]}
        </Pill>
      </div>
      <div className="mt-4 flex items-center justify-between gap-3 border-b border-slate-200 px-5">
        <div role="tablist" aria-label="Case sections" className="flex gap-5">
          {panels.map((name) => <button key={name} type="button" role="tab" aria-selected={panel === name} tabIndex={panel === name ? 0 : -1} aria-controls={`case-${name}`} id={`case-${name}-tab`} onClick={() => setPanel(name)} onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? panels[0] : event.key === "End" ? panels.at(-1) : panels[(panels.indexOf(name) + (event.key === "ArrowRight" ? 1 : -1) + panels.length) % panels.length];
            setPanel(next); document.getElementById(`case-${next}-tab`)?.focus();
          }} className={`border-b-2 pb-3 text-sm font-medium ${panel === name ? "border-sky-700 text-sky-800" : "border-transparent text-slate-500 hover:text-slate-900"}`}>{({summary: "Summary", activity: "Activity", communications: "Communications"})[name]}</button>)}
        </div>
      </div>
      <div id="case-activity" role="tabpanel" aria-labelledby="case-activity-tab" hidden={panel !== "activity"} className="px-5 pb-5"><CaseActivity value={task.activity} /></div>
      {hasCommunications && <div id="case-communications" role="tabpanel" aria-labelledby="case-communications-tab" hidden={panel !== "communications"} className="px-5 pb-1"><CaseCommunications task={task} active={panel === "communications"} manageChase={manageChase} blocked={actionBusy} onReviewCounts={onReviewCounts} onReviewLock={onReviewLock} /></div>}
      <div id="case-summary" role="tabpanel" aria-labelledby="case-summary-tab" hidden={panel !== "summary"} className="p-5">
      {(() => {
        const activity = readChaseActivity(task.activity);
        return <p className="mb-4 text-xs leading-relaxed text-slate-500">{activity ? activity.totalAttempts ? <>{activity.totalAttempts} recorded contact attempt{activity.totalAttempts === 1 ? "" : "s"}{activity.lastAttempt && <> · Last: {CHASE_CHANNELS[activity.lastAttempt.channel]}, {chaseOutcomeLabel(activity.lastAttempt.outcome)} · {chaseAttemptTime(activity.lastAttempt.at)}</>}</> : "No recorded contact attempts." : "Contact activity unavailable"}</p>;
      })()}

      <div className={`rounded-lg border-l-4 px-4 py-3 ${task.state === "needs_review" ? "border-amber-400 bg-amber-50" : "border-sky-500 bg-sky-50"}`}><h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600">Next action</h3><p className="mt-1 text-sm leading-relaxed text-slate-800">{task.instruction}</p></div>

      {task.contact ? (
        <div className="mt-4 rounded-lg border border-slate-200 p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-sky-700">
            <Phone size={13} /> Synthetic manual-call destination
          </p>
          <p className="mt-1 font-mono text-lg text-slate-900">{task.contact.destination}</p>
          <p className="mt-1 text-xs text-sky-800">
            Display only. Use the Communications section when available to review and authorize a text.
          </p>
        </div>
      ) : (
        <div className="mt-4 rounded-lg border border-slate-200 p-3 text-sm text-slate-600">
          <p className="flex items-center gap-1.5 font-medium">
            <LockKeyhole size={15} /> Contact destination withheld
          </p>
          <p className="mt-1 text-xs leading-relaxed text-amber-800">
            No contact destination is available in this surface. Follow the task instruction above.
          </p>
        </div>
      )}

      <details className="mt-3 text-xs leading-relaxed text-slate-500"><summary className="cursor-pointer">Source information</summary><p className="mt-2">{task.systemOfRecord.name} · {task.systemOfRecord.instruction}</p></details>

      {!manageChase ? (
        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
          This worklist is read-only for your assigned role.
        </div>
      ) : !task.open ? (
        <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-800">
          This task is closed. No additional patient contact is needed.
        </div>
      ) : (
        <div className="mt-5 border-t border-slate-200 pt-4">
          <p className="text-sm font-medium text-slate-900">Work this case</p>
          <p className="mt-1 text-xs text-slate-500">
            These controls record work only. They never call, text, email, or write to a source system.
          </p>

          {!task.assigned && may("assign") && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={() => onAction({ action: "assign" }, "Task assigned to you")}
              className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
            >
              <UserCheck size={14} /> Assign to me
            </button>
          )}

          {may("log_attempt") && (
            <form
              className="mt-4 rounded-xl border border-slate-200 p-3"
              onSubmit={(event) => {
                event.preventDefault();
                onAction({
                  action: "log_attempt",
                  channel: "phone",
                  outcome,
                  minutes_spent: Number(minutes),
                  note,
                }, "Manual attempt recorded");
              }}
            >
              <p className="text-xs font-medium uppercase tracking-wide text-slate-600">
                Log a manual phone attempt
              </p>
              <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-[1fr_7rem]">
                <label className="text-xs text-slate-600">
                  Outcome
                  <select
                    value={outcome}
                    onChange={(event) => setOutcome(event.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-sm text-slate-900"
                  >
                    {OUTCOMES.map(([value, label]) => (
                      <option key={value} value={value}>{label}</option>
                    ))}
                  </select>
                </label>
                <label className="text-xs text-slate-600">
                  Minutes
                  <input
                    type="number"
                    min="0"
                    max="1440"
                    step="0.5"
                    required
                    value={minutes}
                    onChange={(event) => setMinutes(event.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-sm text-slate-900"
                  />
                </label>
              </div>
              <label className="mt-2 block text-xs text-slate-600">
                Note (optional)
                <textarea
                  value={note}
                  maxLength={4000}
                  onChange={(event) => setNote(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-slate-200 px-2.5 py-2 text-sm text-slate-900"
                  rows={2}
                />
              </label>
              <button
                type="submit"
                disabled={actionBusy || Number(minutes) < 0 || Number(minutes) > 1440}
                className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-60"
              >
                <Phone size={14} /> Record attempt
              </button>
            </form>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            {may("submitted") && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => setConfirming("submitted")}
                className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-60"
              >
                Mark submitted
              </button>
            )}
            {may("verify") && (
              <button
                type="button"
                disabled={actionBusy}
                onClick={() => setConfirming("verify")}
                className="inline-flex items-center gap-1.5 rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-800 disabled:opacity-60"
              >
                <CheckCircle2 size={14} /> Verify and close
              </button>
            )}
          </div>

          {!may("log_attempt") && !may("submitted") && !may("verify") && (
            <p className="mt-3 text-xs text-amber-800">
              This task requires a reviewed workflow transition before contact can be recorded.
            </p>
          )}

          {confirming && (
            <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50 p-3" role="group" aria-label="Confirm staff action">
              <p className="text-sm font-medium text-amber-950">
                {confirming === "submitted"
                  ? "Confirm that insurance information was submitted for staff review."
                  : "Confirm that staff verified the insurance information in the system of record."}
              </p>
              <p className="mt-1 text-xs text-amber-800">
                This records workflow state only; it does not write to a source system or contact the patient.
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={async () => {
                    const action = confirming;
                    setConfirming(null);
                    await onAction(
                      { action },
                      action === "submitted" ? "Submission recorded" : "Task verified and closed",
                    );
                  }}
                  className="rounded-lg bg-amber-800 px-3 py-1.5 text-sm font-medium text-white hover:bg-amber-900 disabled:opacity-60"
                >
                  Confirm
                </button>
                <button
                  type="button"
                  disabled={actionBusy}
                  onClick={() => setConfirming(null)}
                  className="rounded-lg border border-amber-300 px-3 py-1.5 text-sm text-amber-900 hover:bg-amber-100 disabled:opacity-60"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {actionNotice && <p className="mt-3 text-xs text-emerald-800" role="status">{actionNotice}</p>}
          {actionError && <p className="mt-3 text-xs text-red-700" role="alert">{actionError}</p>}
          {retryAction && (
            <button
              type="button"
              disabled={actionBusy}
              onClick={retryAction}
              className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-red-200 px-3 py-1.5 text-xs font-medium text-red-800 hover:bg-red-50 disabled:opacity-60"
            >
              <RefreshCw size={13} /> Retry the same action safely
            </button>
          )}
        </div>
      )}
      </div>
    </section>
  );
}

export default function CardChase({ live, demoSeed = null, manageChase = false }) {
  const [bucket, setBucket] = useState("");
  const [query, setQuery] = useState("");
  const [worklist, setWorklist] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [seeding, setSeeding] = useState(false);
  const [seedResult, setSeedResult] = useState(null);
  const [seedError, setSeedError] = useState(null);
  const [task, setTask] = useState(null);
  const [taskLoading, setTaskLoading] = useState(false);
  const [taskError, setTaskError] = useState(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [reviewLocked, setReviewLocked] = useState(false);
  const [inboxLocked, setInboxLocked] = useState(false);
  const [inboxRefresh, setInboxRefresh] = useState(0);
  const [actionError, setActionError] = useState(null);
  const [actionNotice, setActionNotice] = useState(null);
  const [retry, setRetry] = useState(null);
  const [selectedChaseId, setSelectedChaseId] = useState(null);
  const selectedChaseIdRef = useRef(null);
  const taskRequestIdRef = useRef(0);
  const [countUpdate, setCountUpdate] = useState({at: null, stale: false});
  const countState = useRef({epoch: 0, caseEpochs: new Map()});
  countState.current = {...countState.current, worklist, bucket,
    blocked: loading || actionBusy || reviewLocked || inboxLocked || !!retry};

  useEffect(() => {
    if (!live || !manageChase) return;
    let epoch;
    return pollVisibleCounts({
      blocked: () => countState.current.blocked || !hasCommunicationCounts(countState.current.worklist),
      read: async (signal) => {
        epoch = countState.current.epoch;
        const observed = epoch, caseEpochs = new Map(countState.current.caseEpochs);
        const ids = listedCaseIds(countState.current.worklist);
        const response = await api("/api/chase/worklist", {method: "POST", signal,
          body: bucket ? {bucket} : {}});
        return {epoch: observed, caseEpochs, counts: readListedCounts(response, ids)};
      },
      accept: ({epoch: observed, caseEpochs, counts}) => {
        if (observed !== countState.current.epoch) return;
        // Only badge counts change. Open task/source, ordering and edits stay put.
        setWorklist((current) => {
          if (observed !== countState.current.epoch) return current;
          const newer = new Set(listedCaseIds(current).filter((id) =>
            caseEpochs.get(id) !== countState.current.caseEpochs.get(id)));
          return mergeListedCounts(current, counts, newer);
        });
        setCountUpdate({at: Date.now(), stale: false});
      },
      failed: () => { if (epoch === countState.current.epoch) setCountUpdate((current) => ({...current, stale: true})); },
    });
  }, [live, manageChase, bucket]);


  const updateReviewCounts = (chaseId, value, refreshQueue = false) => {
    const counts = reviewCounts(value);
    if (!counts || selectedChaseIdRef.current !== chaseId) return;
    const epochs = countState.current.caseEpochs;
    epochs.set(chaseId, (epochs.get(chaseId) || 0) + 1);
    setTask((current) => current?.chaseId === chaseId ? { ...current, communicationReview: counts } : current);
    setWorklist((current) => current ? { ...current, rows: current.rows.map((row) => row.chaseId === chaseId
      ? { ...row, communicationReview: counts } : row) } : current);
    if (refreshQueue) { load(); setInboxRefresh((value) => value + 1); }
  };

  const load = async () => {
    if (!live) return;
    countState.current.epoch++;
    countState.current.caseEpochs = new Map();
    setLoading(true);
    setError(null);
    try {
      const result = await api("/api/chase/worklist", {
        method: "POST",
        body: bucket ? { bucket } : {},
      });
      setWorklist(result);
      setCountUpdate({at: hasCommunicationCounts(result) ? Date.now() : null, stale: false});
    } catch (loadError) {
      setError(`Could not load insurance follow-up work: ${loadError.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    selectedChaseIdRef.current = null;
    setSelectedChaseId(null);
    taskRequestIdRef.current += 1;
    setTask(null);
    setTaskLoading(false);
    setTaskError(null);
    load();
  }, [bucket, live]); // eslint-disable-line react-hooks/exhaustive-deps

  const openTaskById = async (chaseId) => {
    const requestId = ++taskRequestIdRef.current;
    setTask(null);
    setTaskError(null);
    setTaskLoading(true);
    try {
      const result = await api("/api/chase/task", {
        method: "POST",
        body: { chaseId },
      });
      if (requestId !== taskRequestIdRef.current) return;
      setTask(result);
    } catch (loadError) {
      if (requestId !== taskRequestIdRef.current) return;
      setTaskError(`Could not open contact task: ${loadError.message}`);
    } finally {
      if (requestId === taskRequestIdRef.current) setTaskLoading(false);
    }
  };

  const openTask = async (row) => {
    selectedChaseIdRef.current = row.chaseId;
    setSelectedChaseId(row.chaseId);
    setActionError(null);
    setActionNotice(null);
    setRetry(null);
    await openTaskById(row.chaseId);
  };

  const refresh = async () => {
    if (!live || actionBusy || reviewLocked || inboxLocked) return;
    setInboxRefresh((value) => value + 1);
    const chaseId = selectedChaseIdRef.current;
    // Refresh selected-case activity too. openTaskById hides the old detail and
    // discards this response if the user selects another case while it loads.
    await Promise.all([load(), chaseId ? openTaskById(chaseId) : Promise.resolve()]);
  };

  const submitAction = async (
    payload, successMessage, actionId = null, targetChaseId = task?.chaseId,
  ) => {
    if (!manageChase || !targetChaseId || inboxLocked) return;
    const chaseId = targetChaseId;
    setActionBusy(true);
    setActionError(null);
    setActionNotice(null);
    setRetry(null);
    let stableActionId = actionId;
    try {
      stableActionId = stableActionId || crypto.randomUUID();
      await api("/api/chase/action", {
        method: "POST",
        body: { chaseId, action_id: stableActionId, ...payload },
      });
      await load();
      if (selectedChaseIdRef.current === chaseId) {
        setActionNotice(successMessage);
        await openTaskById(chaseId);
      }
    } catch (actionFailure) {
      if (selectedChaseIdRef.current !== chaseId) return;
      const retryable = !actionFailure?.status
        || actionFailure.status === 429
        || actionFailure.status >= 500;
      setActionError(
        actionFailure?.status === 409
          ? "This task changed while you were working. Refresh it before trying again."
          : actionFailure?.status === 422
            ? "That action was not valid for the current task. Review the fields and try again."
            : "The action could not be confirmed. No automatic contact was sent.",
      );
      if (retryable && stableActionId) {
        setRetry({
          payload, successMessage, actionId: stableActionId, chaseId,
        });
      }
    } finally {
      setActionBusy(false);
    }
  };

  const seedSyntheticDemo = async () => {
    if (!live || !demoSeed?.seedDigest) return;
    setSeeding(true);
    setSeedError(null);
    setSeedResult(null);
    let confirmed = false;
    try {
      const result = await api("/api/chase/demo-seed", {
        method: "POST",
        body: { seedDigest: demoSeed.seedDigest },
      });
      if (!seedConfirmationIsSafe(result, demoSeed)) {
        throw new Error("synthetic demo seed confirmation did not match");
      }
      setSeedResult(result);
      confirmed = true;
      setTask(null);
      setTaskError(null);
    } catch (seedFailure) {
      setSeedError(syntheticSeedErrorMessage(seedFailure));
    } finally {
      setSeeding(false);
    }
    if (confirmed) await load();
  };

  if (!live) {
    return (
      <div className="rounded-2xl border border-amber-200 bg-amber-50 p-5 text-sm text-amber-900 shadow-sm">
        Insurance follow-up is unavailable while the service is offline. No mock patient task was loaded.
      </div>
    );
  }

  const hasInbox = worklist?.communications?.available === true && worklist.communications.mode === "durable";
  const counts = worklist?.counts;
  const outbound = worklist?.outbound;
  const shown = (worklist?.rows || []).filter((row) =>
    [row.patientLabel, row.chaseId, row.bucket, row.state].join(" ").toLowerCase().includes(query.trim().toLowerCase()));
  const number = (value) => Number.isSafeInteger(value) && value >= 0 ? value : "—";
  return (
    <div>
      <header className="mb-4 flex flex-wrap items-end justify-between gap-4">
        <div><h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight text-slate-950"><CreditCard size={23} className="text-sky-700" />Insurance follow-up</h1><p className="mt-1 text-sm text-slate-500">One queue for missing cards, expired plans, and eligibility exceptions.</p></div>
        <div className="flex items-center gap-5">
          <dl className="flex gap-5 text-sm">{[["Open", counts?.open], ["Needs review", counts?.needs_person], ["Unassigned", counts?.unassigned]].map(([label, value]) => <div key={label}><dt className="text-xs text-slate-500">{label}</dt><dd className="font-semibold text-slate-900">{number(value)}</dd></div>)}</dl>
          <button onClick={refresh} disabled={loading || taskLoading || actionBusy || reviewLocked || inboxLocked} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-600 hover:bg-slate-50 disabled:opacity-60"><RefreshCw size={14} className={loading || taskLoading ? "animate-spin" : ""} />Refresh</button>
        </div>
      </header>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-500">
        {outbound && <details><summary className="cursor-pointer">{outbound.automaticContact ? "Outreach automation active" : "Manual outreach"} · Status details</summary><p className="mt-2">Outbound automation is {outbound.automaticContact ? "active" : "off"}; transport is {outbound.transport}. Manual actions only record work. Text drafts require separate staff authorization in Communications when available.</p></details>}
        {demoSeed && <details><summary className="cursor-pointer">Demo setup</summary><div className="mt-2 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white p-3"><p>Admin only. Fixed {demoSeed.caseCount}-case synthetic pack.</p><button onClick={seedSyntheticDemo} disabled={seeding} className="rounded-md border border-slate-300 px-2 py-1 font-medium disabled:opacity-50">{seeding ? "Loading synthetic tasks…" : "Load synthetic demo tasks"}</button>{seedResult && <p role="status">{syntheticSeedConfirmationMessage(seedResult)}</p>}{seedError && <p role="alert">{seedError}</p>}</div></details>}
      </div>
      {error && <p role="alert" className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {manageChase && hasCommunicationCounts(worklist) && <p role={countUpdate.stale ? "status" : undefined}
        className={`mb-3 text-xs ${countUpdate.stale ? "text-amber-800" : "text-slate-500"}`}>
        {countUpdate.stale ? "Communication counts may be out of date. Refresh the board to try again."
          : "Communication counts update automatically while this board is visible."}
        {countUpdate.at && <> Last updated <time dateTime={new Date(countUpdate.at).toISOString()}>{new Date(countUpdate.at).toLocaleTimeString()}</time>.</>}
      </p>}
      {manageChase && hasInbox && <UnassignedTexts blocked={actionBusy || reviewLocked} refreshVersion={inboxRefresh} onLock={setInboxLocked}
        onReviewed={async () => { await load(); const selected = selectedChaseIdRef.current; if (selected) await openTaskById(selected); }} />}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(25rem,0.9fr)_minmax(0,1.1fr)]">
        <section aria-label="Insurance follow-up queue" className="min-w-0 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 p-4">
            <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold text-slate-900">Work queue</h2><span className="text-xs text-slate-500">{shown.length} shown</span></div>
            <label className="flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2"><Search size={15} className="text-slate-400" /><input type="search" aria-label="Search cases" placeholder="Search cases…" value={query} onChange={(event) => setQuery(event.target.value)} className="min-w-0 flex-1 bg-transparent text-sm outline-none" /></label>
            <div className="mt-3 flex flex-wrap gap-1.5">{FILTERS.map(([value, label]) => <button type="button" key={value || "all"} onClick={() => setBucket(value)} disabled={actionBusy || inboxLocked} aria-pressed={bucket === value} className={`rounded-md px-2.5 py-1.5 text-xs font-medium disabled:opacity-50 ${bucket === value ? "bg-sky-800 text-white" : "text-slate-600 hover:bg-slate-100"}`}>{label}{counts ? ` · ${number(value ? counts.by_bucket?.[value] : counts.open)}` : ""}</button>)}</div>
          </div>
          <div className="overflow-x-auto">
            <div className="min-w-[440px]">
              <div className="grid grid-cols-[minmax(0,1.7fr)_minmax(0,0.8fr)_5rem_3rem] gap-2 border-b border-slate-200 bg-slate-50 px-4 py-2 text-[10px] font-semibold uppercase tracking-wide text-slate-500"><span>Case / status</span><span>Reason</span><span>Visit</span><span className="text-right">Touches</span></div>
              {loading && !worklist ? <p className="p-5 text-sm text-slate-500">Loading follow-up work…</p> : !shown.length ? <p className="p-5 text-sm text-slate-500">{query ? "No cases match your search." : "No open tasks match this filter."}</p> : <div className="max-h-[650px] divide-y divide-slate-100 overflow-y-auto">{shown.map((row) => {
                const [statusLabel, statusTone] = STATUS[row.state] || [row.state, "bg-slate-100 text-slate-600 ring-slate-300"];
                return <button type="button" key={row.chaseId} onClick={() => openTask(row)} disabled={actionBusy || inboxLocked} aria-pressed={selectedChaseId === row.chaseId} className={`grid w-full grid-cols-[minmax(0,1.7fr)_minmax(0,0.8fr)_5rem_3rem] items-center gap-2 border-l-[3px] px-3 py-3 text-left disabled:opacity-60 ${selectedChaseId === row.chaseId ? "border-sky-700 bg-sky-50" : "border-transparent hover:bg-slate-50"}`}>
                  <span className="min-w-0"><span className="block truncate text-sm font-medium text-slate-900">{row.patientLabel}</span><span className="mt-1 inline-block"><Pill tone={statusTone}>{statusLabel}</Pill></span>{reviewCounts(row.communicationReview)?.pending > 0 && <span className="mt-1 block text-[11px] font-medium text-amber-800">{row.communicationReview.pending} communication event{row.communicationReview.pending === 1 ? "" : "s"} to review</span>}</span>
                  <span className="text-xs text-slate-500">{row.bucket}<span className="mt-1 block text-[10px]">Priority {number(row.priority)}</span></span>
                  <span className="text-xs text-slate-500">{row.appointmentAt ? row.appointmentAt.slice(5, 10) : "—"}</span><span className="text-right text-sm font-medium text-slate-600">{number(row.touches)}</span>
                </button>;
              })}</div>}
            </div>
          </div>
        </section>

        <FollowUpTask
          onReviewCounts={updateReviewCounts}
          onReviewLock={setReviewLocked}
          key={task?.chaseId || "empty-task"}
          task={task}
          loading={taskLoading}
          error={taskError}
          manageChase={manageChase}
          actionBusy={actionBusy || inboxLocked}
          actionError={actionError}
          actionNotice={actionNotice}
          retryAction={retry ? () => submitAction(
            retry.payload, retry.successMessage, retry.actionId, retry.chaseId,
          ) : null}
          onAction={submitAction}
        />
      </div>
    </div>
  );
}
