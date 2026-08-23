"""Source contracts for the dependency-light synthetic demo frontend."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "cosmaneura_billing" / "frontend" / "src"
INDEX = (FRONTEND.parent / "index.html").read_text(encoding="utf-8")
APP = (FRONTEND / "App.jsx").read_text(encoding="utf-8")
BILLING = (FRONTEND / "BillingReview.jsx").read_text(encoding="utf-8")
CODER = (FRONTEND / "CoderReview.jsx").read_text(encoding="utf-8")
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
    assert "!selected" in APP
    assert "No synthetic demo encounter is loaded" in APP
    assert "No mock claim or expected code has been substituted." in APP
    assert "billing service offline · no mock claim loaded" in APP
    assert "engine offline · mock data" not in APP


def test_synthetic_fixture_is_read_only_and_uses_only_the_stored_billing_route():
    assert (
        'synthetic_demo_release_candidate: "Validated synthetic release-candidate fixture"'
        in BILLING
    )
    assert 'athena_preview_synthetic: "Athena Preview synthetic fixture"' in BILLING
    assert "enc.readOnly === true" in BILLING
    assert "SYNTHETIC_DATA_LABELS[enc.dataClass]" in BILLING
    assert "{syntheticDataLabel} · documentation is read-only" in BILLING
    assert 'enc.encounterId === "SYN-RC-0001"' not in BILLING
    assert 'startsWith("SYN-RC-")' not in BILLING
    assert "documentation is read-only" in BILLING
    assert "readOnly={documentationReadOnly}" in BILLING

    request_start = BILLING.index("const j = documentationReadOnly")
    request_end = BILLING.index("setResult(j);", request_start)
    request_branch = BILLING[request_start:request_end]
    stored_branch, editable_branch = request_branch.split(
        ': await api("/api/billing"', maxsplit=1
    )

    assert 'api("/api/encounters/billing"' in stored_branch
    assert 'method: "POST"' in stored_branch
    assert "body: { encounterId: enc.encounterId }" in stored_branch
    assert "body:" in editable_branch


def test_billing_and_coder_skip_requests_without_an_encounter_id():
    billing_guard = BILLING.index("if (!encounterId) { setEnc(null); return; }")
    billing_request = BILLING.index(
        'api("/api/encounters/detail"', billing_guard
    )
    assert billing_guard < billing_request

    coder_guard = CODER.index(
        "if (!encounterId) { reviewId.current = null; return; }"
    )
    coder_request = CODER.index(
        'api("/api/encounters/coder-view"',
        coder_guard,
    )
    assert coder_guard < coder_request


def test_frontend_never_places_an_encounter_identifier_in_a_request_target():
    assert "encodeURIComponent(encounterId)" not in BILLING + CODER
    assert "encodeURIComponent(enc.encounterId)" not in BILLING + CODER
    assert "`/api/encounters/${" not in BILLING + CODER


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
    assert 'role="tablist"' in APP
    assert 'role="tab"' in APP
    assert "patientLabel}`" not in CHASE
    assert "chaseId}`" not in CHASE


def test_degraded_results_suppress_claim_cards_and_feedback_actions():
    billing_degraded = BILLING.index("result?.degraded ?")
    billing_normal = BILLING.index(": result ?", billing_degraded)
    billing_banner = BILLING[billing_degraded:billing_normal]
    assert "Inference is unavailable for this encounter" in billing_banner
    assert "No claim suggestions or feedback are shown" in billing_banner
    assert "ProviderCard" not in billing_banner
    assert "BillerCard" not in billing_banner

    coder_degraded = CODER.index("if (engine.degraded)")
    coder_normal = CODER.index("const set =", coder_degraded)
    coder_banner = CODER[coder_degraded:coder_normal]
    assert "No codes proposed for this encounter" in coder_banner
    assert "No claim suggestions or feedback actions are shown" in coder_banner
    assert "Submit &amp; next" not in coder_banner
