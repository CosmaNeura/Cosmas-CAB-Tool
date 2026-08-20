"""Source contracts for the dependency-light synthetic demo frontend."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "cosmaneura_billing" / "frontend" / "src"
APP = (FRONTEND / "App.jsx").read_text(encoding="utf-8")
BILLING = (FRONTEND / "BillingReview.jsx").read_text(encoding="utf-8")
CODER = (FRONTEND / "CoderReview.jsx").read_text(encoding="utf-8")


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
    assert 'enc.encounterId === "SYN-RC-0001"' in BILLING
    assert 'startsWith("SYN-RC-")' not in BILLING
    assert "documentation is read-only" in BILLING
    assert "readOnly={isSyntheticReleaseCandidate}" in BILLING

    request_start = BILLING.index("const j = isSyntheticReleaseCandidate")
    request_end = BILLING.index("setResult(j);", request_start)
    request_branch = BILLING[request_start:request_end]
    stored_branch, editable_branch = request_branch.split(
        ': await api("/api/billing"', maxsplit=1
    )

    assert (
        "`/api/encounters/${encodeURIComponent(enc.encounterId)}/billing`"
        in stored_branch
    )
    assert 'method: "POST"' in stored_branch
    assert "body:" not in stored_branch
    assert "body:" in editable_branch


def test_billing_and_coder_skip_requests_without_an_encounter_id():
    billing_guard = BILLING.index("if (!encounterId) { setEnc(null); return; }")
    billing_request = BILLING.index(
        "api(`/api/encounters/${encodeURIComponent(encounterId)}`)", billing_guard
    )
    assert billing_guard < billing_request

    coder_guard = CODER.index(
        "if (!encounterId) { reviewId.current = null; return; }"
    )
    coder_request = CODER.index(
        "api(`/api/encounters/${encodeURIComponent(encounterId)}/coder-view`)",
        coder_guard,
    )
    assert coder_guard < coder_request


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
