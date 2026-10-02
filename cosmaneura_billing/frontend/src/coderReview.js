export const UNREVIEWED = "unreviewed";

const clean = (value) => typeof value === "string" ? value.trim() : "";
const REVIEW_KINDS = new Set(["EM", "ICD10", "PROCEDURE", "MODIFIER"]);

function hasCompletenessContract(engine) {
  return Boolean(
    engine?.completeness
    && Object.hasOwn(engine.completeness, "complete")
    && [true, false, null].includes(engine.completeness.complete)
    && Array.isArray(engine.completeness.results),
  );
}

export function canPrepareCoderPreview(engine) {
  return Boolean(
    hasCompletenessContract(engine)
    && engine.completeness.complete === true
    && !engine.degraded
    && !engine.contractViolations,
  );
}

export function hasReviewEvidence(row) {
  return Boolean(
    clean(row?.code)
    && Array.isArray(row?.evidenceSpans)
    && row.evidenceSpans.some((span) => clean(span?.source) && clean(span?.text)),
  );
}

export function reviewableCoderView(engine) {
  if (!engine) return null;
  const validKindAndCode = (row) => (
    Boolean(clean(row?.code)) && REVIEW_KINDS.has(row?.kind)
  );
  const reviewable = (row, expectedKind = null) => (
    hasReviewEvidence(row)
    && REVIEW_KINDS.has(row.kind)
    && (expectedKind == null || row.kind === expectedKind)
  );
  const excluded = engine.withheld || [];
  const completenessContractValid = hasCompletenessContract(engine);
  const permittedSuppression = (row) => (
    ["held", "rejected"].includes(row?.status)
    && validKindAndCode(row)
    && clean(row.why)
    && !hasReviewEvidence(row)
  );
  const contractViolations = [
    ...(!completenessContractValid ? [engine.completeness] : []),
    ...(engine.em && (!reviewable(engine.em, "EM") || !clean(engine.em.why)) ? [engine.em] : []),
    ...(engine.dx || []).filter((row) => !reviewable(row, "ICD10") || !clean(row.why)),
    ...excluded.filter((row) => (
      !permittedSuppression(row)
      && (
        !reviewable(row)
        || !clean(row.why)
        || !["held", "rejected"].includes(row.status)
      )
    )),
  ].length;
  const suppressedExclusions = excluded.filter(permittedSuppression).length;
  return {
    ...engine,
    em: reviewable(engine.em, "EM") ? engine.em : null,
    dx: (engine.dx || []).filter((row) => reviewable(row, "ICD10")),
    withheld: (engine.withheld || []).filter(
      (row) => reviewable(row) && clean(row.why),
    ),
    contractViolations,
    suppressedExclusions,
    completenessContractValid,
  };
}

export function capturedRecommendations(engine) {
  if (!engine) return [];
  return [
    ...(engine.em ? [{ key: "em", kind: "EM", row: engine.em }] : []),
    ...(engine.dx || []).map((row, index) => ({
      key: `dx:${index}`,
      kind: "ICD10",
      row,
    })),
  ];
}

export function excludedRecommendations(engine) {
  return (engine?.withheld || []).map((row, index) => ({
    key: `excluded:${index}`,
    kind: row.kind,
    row,
  }));
}

export function initialCoderDraft(engine) {
  return {
    decisions: Object.fromEntries(capturedRecommendations(engine).map(({ key }) => [
      key,
      { action: UNREVIEWED, finalCode: "", reasonCode: "", rationale: "" },
    ])),
  };
}

function issue(issues, key, message) {
  issues.push({ key, message });
}

export function validateCoderDraft(engine, decisions, additions) {
  const issues = [];
  const decisionCount = capturedRecommendations(engine).length + additions.length;

  if (decisionCount > 500) {
    issue(issues, "recommendations", "This review exceeds the 500-decision server limit.");
  }
  if (engine && capturedRecommendations(engine).length === 0 && additions.length === 0) {
    issue(issues, "recommendations", "No evidence-supported recommendation or human-added code is available to review.");
  }
  if (engine && !hasCompletenessContract(engine)) {
    issue(issues, "completeness", "The completeness response is invalid. Reload before recording this review.");
  }

  capturedRecommendations(engine).forEach(({ key, row }) => {
    const decision = decisions[key];
    if (!decision || decision.action === UNREVIEWED) {
      issue(issues, key, `Choose accept, edit, or reject for ${row.code}.`);
      return;
    }
    if (decision.action === "modify") {
      if (!clean(decision.finalCode) || clean(decision.finalCode) === clean(row.code)) {
        issue(issues, key, `Enter a different final code for ${row.code}.`);
      }
      if (!clean(decision.reasonCode)) issue(issues, key, `Choose an edit reason for ${row.code}.`);
      if (!clean(decision.rationale)) issue(issues, key, `Explain the edit to ${row.code}.`);
      if (rationaleNote(decision.rationale).length > 4_000) issue(issues, key, `Shorten the rationale for ${row.code}.`);
    }
    if (decision.action === "reject") {
      if (!clean(decision.reasonCode)) issue(issues, key, `Choose a rejection reason for ${row.code}.`);
      if (!clean(decision.rationale)) issue(issues, key, `Explain why ${row.code} is rejected.`);
      if (rationaleNote(decision.rationale).length > 4_000) issue(issues, key, `Shorten the rationale for ${row.code}.`);
    }
  });

  additions.forEach((addition) => {
    const key = `addition:${addition.id}`;
    if (!clean(addition.code)) issue(issues, key, "Enter the human-added code.");
    if (clean(addition.code).length > 32) issue(issues, key, "Shorten the human-added code to 32 characters.");
    if (!clean(addition.rationale)) issue(issues, key, "Explain why the code is being added.");
    if (!clean(addition.evidence)) issue(issues, key, "Add supporting documentation evidence.");
    if (rationaleNote(addition.rationale, addition.evidence).length > 4_000) {
      issue(issues, key, "Shorten the rationale and evidence to fit the review record.");
    }
  });

  return { valid: issues.length === 0, issues };
}

function rationaleNote(rationale, evidence = "", originalDisposition = "") {
  return [
    `Rationale: ${clean(rationale)}`,
    ...(clean(evidence) ? [`Evidence: ${clean(evidence)}`] : []),
    ...(clean(originalDisposition)
      ? [`Original engine disposition: ${clean(originalDisposition)}`]
      : []),
  ].join("\n");
}

export function buildFeedbackPayload({
  engine, decisions, additions, reviewId, seconds,
}) {
  const captured = capturedRecommendations(engine).map(({ key, kind, row }) => {
    const decision = decisions[key];
    return {
      kind,
      engine_code: row.code,
      action: decision.action,
      final_code: decision.action === "reject" ? null : (
        decision.action === "accept" ? row.code : clean(decision.finalCode)
      ),
      reason_code: decision.action === "accept" ? null : decision.reasonCode,
      note: decision.action === "accept" ? "" : rationaleNote(decision.rationale),
    };
  });
  const additionReason = (kind) => ({
    ICD10: "dx_missed_by_engine",
    PROCEDURE: "other",
    MODIFIER: "modifier_required",
  }[kind] || "other");
  const humanAdded = additions.map((addition) => ({
    kind: addition.kind,
    engine_code: null,
    action: "add",
    final_code: clean(addition.code),
    reason_code: addition.sourceDisposition ? "other" : additionReason(addition.kind),
    note: rationaleNote(
      addition.rationale, addition.evidence, addition.sourceDisposition,
    ),
  }));
  const emDecision = engine.em ? decisions.em : null;

  return {
    review_id: reviewId,
    encounter_id: engine.encounterId,
    review_context: engine.reviewContext,
    encounter_version: engine.encounterVersion,
    submitted_em: engine.submittedEm || null,
    engine_em: engine.em?.code || null,
    final_em: emDecision && emDecision.action !== "reject"
      ? (emDecision.action === "accept" ? engine.em.code : clean(emDecision.finalCode))
      : null,
    decisions: [...captured, ...humanAdded],
    time_spent_seconds: Math.min(86_400, Math.max(0, Math.floor(seconds))),
    overall_note: "",
  };
}

export function coderQueueState({ engine, draftValid, saved }) {
  if (saved) return canPrepareCoderPreview(engine)
    ? "Reviewed/prepared" : "Reviewed — completeness unresolved";
  if (engine?.completeness?.complete === false) return "Completeness blocked";
  if (engine && engine.completeness?.complete !== true) return "Completeness not checked";
  if (engine?.degraded) return "Awaiting provider clarification";
  return draftValid ? "Clean" : "Needs review";
}

export function coderErrorMessage(error, phase) {
  if (phase === "verify") {
    return "The prior review receipt remains confirmed, but exact replay verification could not be completed. Nothing was transmitted.";
  }
  if (error?.status === 401) return "Your session expired. Sign in again before continuing.";
  if (error?.status === 403) return "Your assigned role cannot perform this coder action.";
  if (error?.status === 404) return "This encounter is no longer available. Refresh the queue.";
  if (error?.status === 409) {
    return "The encounter or review version changed. Reload before saving; the frozen draft remains visible and nothing was transmitted.";
  }
  if (error?.status === 422) return "The server rejected the review fields. Recheck every decision and rationale.";
  return phase === "load"
    ? "The coder review could not be loaded. No browser fallback or canned result was substituted."
    : "The review could not be saved. The exact frozen payload can be retried; nothing was transmitted.";
}
