"""Source contracts for the dependency-light synthetic demo frontend."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "cosmaneura_billing" / "frontend" / "src"
INDEX = (FRONTEND.parent / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "App.jsx").read_text(encoding="utf-8")
BILLING = (FRONTEND / "BillingReview.jsx").read_text(encoding="utf-8")
QUEUE = (FRONTEND / "ProviderQueue.jsx").read_text(encoding="utf-8")
PROVIDER = (FRONTEND / "providerBilling.js").read_text(encoding="utf-8")
CODER = (FRONTEND / "CoderReview.jsx").read_text(encoding="utf-8")
CODER_LOGIC = (FRONTEND / "coderReview.js").read_text(encoding="utf-8")
CHASE = (FRONTEND / "CardChase.jsx").read_text(encoding="utf-8")
SEED = (FRONTEND / "syntheticDemoSeed.js").read_text(encoding="utf-8")


def test_cosmas_is_the_product_brand_and_cosmaneura_is_the_company():
    assert '<meta name="application-name" content="Cosmas" />' in INDEX
    assert "<title>Cosmas · Billing review</title>" in INDEX
    assert ">Cosmas · Billing review</h1>" in APP
    assert "by CosmaNeura" in APP
    assert "function BrandLockup()" in APP
    assert "function BrandedMessage({ children })" in APP
    assert APP.count("<BrandedMessage>") == 2
    assert "CosmaNeura · Billing review" not in INDEX + APP


def test_app_waits_for_a_list_selection_and_fails_safe_when_the_list_is_empty():
    assert 'const [selected, setSelected] = useState(null);' in APP
    assert "ENC-100237" not in APP
    assert "!encountersLoaded" in APP
    assert "encounterId={selected}" in APP
    assert "No synthetic demo encounter is loaded" in APP
    assert "No mock claim or expected code has been substituted." in APP
    assert "billing service offline · no mock claim loaded" in APP
    assert "engine offline · mock data" not in APP
    assert "encs[0]" not in APP
    assert "Select an encounter to begin" in BILLING


def test_synthetic_fixture_is_read_only_and_uses_only_the_stored_billing_route():
    assert (
        'synthetic_demo_release_candidate: "Validated synthetic release-candidate fixture"'
        in PROVIDER
    )
    assert 'athena_preview_synthetic: "Athena Preview synthetic fixture"' in PROVIDER
    assert "detail.readOnly === true" in BILLING
    assert "sourceLabel(encounter.dataClass)" in BILLING
    assert 'startsWith("SYN-RC-")' not in BILLING + QUEUE + PROVIDER
    assert "Read-only documentation" in BILLING
    assert "available.filter(isSyntheticDemoEncounter)" in APP
    assert "!isSyntheticDemoEncounter(detail)" in BILLING
    assert 'api("/api/encounters/billing-tab"' in BILLING
    assert 'api("/api/encounters/billing"' not in BILLING
    assert 'api("/api/billing"' not in BILLING
    assert 'method: "POST"' in BILLING
    assert "body: { encounterId: requestedEncounterId }" in BILLING
    assert "mockBillingApi" not in BILLING
    assert "DEMO_ENCOUNTER" not in BILLING


def test_billing_and_coder_skip_requests_without_an_encounter_id():
    billing_guard = BILLING.index("if (!selectedId)")
    billing_request = BILLING.index(
        'api("/api/encounters/detail"', billing_guard
    )
    assert billing_guard < billing_request

    coder_guard = CODER.index("if (!encounterId) return cancelRequests;")
    coder_request = CODER.index(
        'api("/api/encounters/coder-view"',
        coder_guard,
    )
    assert coder_guard < coder_request


def test_frontend_never_places_an_encounter_identifier_in_a_request_target():
    assert "encodeURIComponent" not in BILLING + QUEUE + CODER
    assert "`/api/encounters/${" not in BILLING + QUEUE + CODER


def test_coder_review_is_explicit_evidence_bound_and_never_falls_back_to_a_mock():
    assert "FALLBACK_ENGINE" not in CODER
    assert "No browser fallback or canned result was loaded" in CODER
    assert "UNREVIEWED" in CODER + CODER_LOGIC
    assert "Each row starts unreviewed" in CODER
    assert "Accept" in CODER and "Edit" in CODER and "Reject" in CODER
    assert "Supporting documentation evidence" in CODER
    assert "contractViolations > 0" in CODER
    assert "suppressedExclusions" in CODER + CODER_LOGIC
    assert "not shown because no cited documentation evidence was returned" in CODER
    assert "validateCoderDraft" in CODER
    assert "reviewableCoderView" in CODER
    assert 'aria-pressed={active}' in CODER
    assert 'role="group"' in CODER


def test_coder_review_rejects_stale_or_untrusted_responses_and_freezes_exact_retry():
    assert "new AbortController()" in CODER
    assert "requestId !== loadRequest.current" in CODER
    assert "response.encounterId !== encounterId" in CODER
    assert "!isSyntheticDemoEncounter(response)" in CODER
    assert "response.dataClass !== listed.dataClass" in CODER
    assert "engine.encounterId !== encounterId" in CODER
    assert "engine?.encounterId === encounterId" in CODER
    assert "pendingPayload.current = buildFeedbackPayload" in CODER
    assert "const payload = pendingPayload.current" in CODER
    assert "requestId !== saveRequest.current" in CODER
    assert "response.reviewId !== payload.review_id" in CODER
    assert "setFrozen(true)" in CODER
    assert "savingRef.current" in CODER
    assert "onNavigationLockChange(true)" in CODER
    assert "selectionDisabled={frozen && !receipt}" in CODER
    assert 'addEventListener("beforeunload"' in CODER
    assert "Reload after confirmed conflict" in CODER
    assert "Sign in again in a new tab" in CODER
    assert "[403, 404, 409].includes(saveErrorStatus)" in CODER
    assert "The server outcome is unknown" in CODER
    assert "Discard frozen draft" not in CODER
    assert "coderReviewLocked && view === \"coder\"" in APP
    assert APP.count("onNavigationLockChange={setCoderReviewLocked}") == 1
    coder_call = APP.split("<CoderReview", 1)[1]
    assert "onNavigationLockChange={setCoderReviewLocked}" in coder_call


def test_coder_copy_describes_feedback_and_prepared_preview_without_transmission_claims():
    forbidden = ("Suggested claim", "Submit &amp; next", "Bill it", "Claim-ready")
    assert not any(term in CODER for term in forbidden)
    assert "Prepared draft — not transmitted" in CODER
    assert "Save review and prepare preview" in CODER
    assert "This action writes coder feedback only" in CODER
    assert "No claim, EHR update, Athena write" in CODER
    assert "server-owned versioned packet is a separate future slice" in CODER
    assert "JSON.stringify" not in CODER
    assert "reviewContext}</" not in CODER
    assert "prior review receipt remains confirmed" in CODER_LOGIC


def test_coder_validation_is_labelled_without_reactive_focus_stealing():
    assert 'aria-invalid={invalid || undefined}' in CODER
    assert "aria-describedby={describedBy}" in CODER
    assert "<RowIssues id={errorId}" in CODER
    assert "requestAnimationFrame(() => validationRef.current?.focus())" in CODER
    assert "[showValidation, validation.issues.length]" not in CODER
    assert 'view === "coder" && encounters.length > 0' not in APP


def test_provider_queue_and_persistent_safety_boundary_are_explicit():
    for label in (
        "Synthetic data", "Decision support only", "Human review required",
        "No EHR or claim transmission", "Patient outreach disabled",
    ):
        assert label in APP
    for heading in (
        "Encounter", "Visit type", "Review state", "Completeness",
        "Coder review", "Provenance", "Processing",
    ):
        assert heading in QUEUE
    assert "session; they do not claim that a review was saved or transmitted" in QUEUE
    assert 'aria-current={selected ? "true" : undefined}' in QUEUE


def test_provider_review_uses_rich_contract_and_rejects_stale_cross_encounter_results():
    assert 'api("/api/completeness"' in BILLING
    assert 'api("/api/encounters/billing-tab"' in BILLING
    assert "review.visit_id !== requestedEncounterId" in BILLING
    assert "selectedId !== requestedEncounterId" in BILLING
    assert "detail.id !== selectedId" in BILLING
    assert "controller.abort()" in BILLING
    assert "clinical_note_modified !== false" in BILLING
    assert "!isSyntheticDemoEncounter(review)" in BILLING
    assert "review.dataClass !== encounter.dataClass" in BILLING
    assert "No browser fallback or canned result was substituted." in BILLING


def test_provider_copy_and_completeness_wording_never_imply_transmission_or_saved_overrides():
    assert "Copy suggested codes" in BILLING
    assert "Copy prepared Athena-friendly block" in BILLING
    assert "copied — not sent" in BILLING
    assert "Nothing was sent" in BILLING
    assert "this view does not create or persist them" in BILLING
    assert "Documentation completeness blocks a ready state" in BILLING
    assert "report.complete && attestation" in BILLING
    assert "Apply reasoned override" not in BILLING


def test_server_capabilities_control_the_three_tabs_and_encounter_loading():
    assert 'api("/api/session")' in APP
    assert 'views.includes("provider")' in APP
    assert 'views.includes("coder")' in APP
    assert 'views.includes("chase")' in APP
    assert 'session.views.includes("provider") || session.views.includes("coder")' in APP
    assert "session.capabilities?.manageChase === true" in APP
    assert "manageChase={manageChase}" in APP
    assert "Provider &amp; biller" not in APP
    assert "Card chase" in APP


def test_admin_only_server_capability_drives_digest_pinned_synthetic_seed():
    assert "setChaseDemoSeed(session.actions?.seedSyntheticChase || null)" in APP
    assert "demoSeed={chaseDemoSeed}" in APP
    assert "demoSeed && (" in CHASE
    assert "Admin only." in CHASE
    assert 'api("/api/chase/demo-seed"' in CHASE
    assert "body: { seedDigest: demoSeed.seedDigest }" in CHASE
    assert "seedConfirmationIsSafe(result, demoSeed)" in CHASE
    assert "syntheticSeedConfirmationMessage(seedResult)" in CHASE
    assert "result?.installedCaseCount === descriptor.caseCount" in SEED
    assert "result.openTaskCount <= result.installedCaseCount" in SEED
    assert "outbound?.schedulerEnabled === false" in SEED
    assert "outbound?.patientMessagingEnabled === false" in SEED
    assert "outbound?.operationallyActivated === false" in SEED
    assert "outbound?.automaticContact === false" in SEED
    assert 'outbound?.transport === "record_only"' in SEED
    assert "syntheticSeedErrorMessage(seedFailure)" in CHASE
    assert "seedFailure.message" not in CHASE
    assert "No duplicates were added." in SEED
    assert "await load()" in CHASE
    assert "fc207ef1419882f48dce5b12dae05233de2f99d9ea930ee5eb84d84e659d23ff" not in APP + CHASE
    assert "console." not in CHASE + SEED


def test_card_chase_uses_fixed_post_routes_and_never_initiates_contact():
    assert 'api("/api/chase/worklist"' in CHASE
    assert 'api("/api/chase/task"' in CHASE
    assert 'api("/api/chase/demo-seed"' in CHASE
    assert 'api("/api/chase/action"' in CHASE
    assert 'method: "POST"' in CHASE
    assert "body: { chaseId }" in CHASE
    assert "`/api/chase/${" not in CHASE
    assert "encodeURIComponent" not in CHASE
    assert "tel:" not in CHASE
    assert "mailto:" not in CHASE
    assert "does not dial, text, email, or open an external link" in CHASE
    assert "They never call, text, email, or write to a source system." in CHASE
    assert "it does not write to a source system or contact the patient" in CHASE
    assert "crypto.randomUUID()" in CHASE
    assert "retry.actionId" in CHASE
    assert "retry.chaseId" in CHASE
    assert "selectedChaseIdRef.current === chaseId" in CHASE
    assert "requestId !== taskRequestIdRef.current" in CHASE
    assert 'task.allowedActions.includes(action)' in CHASE
    assert "Retry the same action safely" in CHASE
    assert 'action: "assign"' in CHASE
    assert 'action: "log_attempt"' in CHASE
    assert 'setConfirming("submitted")' in CHASE
    assert 'setConfirming("verify")' in CHASE
    assert "This worklist is read-only for your assigned role." in CHASE
    assert 'aria-pressed={selectedChaseId === row.chaseId}' in CHASE
    assert '<nav' in APP
    assert 'aria-label="Workspace"' in APP
    assert 'role="tablist"' not in APP
    assert 'role="tab"' not in APP
    assert "patientLabel}`" not in CHASE
    assert "chaseId}`" not in CHASE


def test_degraded_results_suppress_claim_cards_and_feedback_actions():
    billing_degraded = BILLING.index("result?.degraded ?")
    billing_normal = BILLING.index(": result ?", billing_degraded)
    billing_banner = BILLING[billing_degraded:billing_normal]
    assert "Inference is unavailable for this encounter" in billing_banner
    assert "No code recommendations, copy actions, or prepared block are shown" in billing_banner
    assert "CandidateGroup" not in billing_banner

    coder_degraded = CODER.index(") : engine.degraded ? (")
    coder_normal = CODER.index(") : (", coder_degraded)
    coder_banner = CODER[coder_degraded:coder_normal]
    assert "No codes proposed for this encounter" in coder_banner
    assert "No code recommendations, prepared draft, or feedback actions are shown" in coder_banner
    assert "Submit &amp; next" not in coder_banner
