import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build_and_deploy.yml"
VERIFIER = REPO_ROOT / ".github" / "scripts" / "verify-cab-release.sh"


def _job_block(workflow: str, name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert match, f"missing job {name}"
    return match.group(0)


def _mapping_keys(workflow: str, heading: str) -> set[str]:
    lines = workflow.splitlines()
    start = lines.index(f"{heading}:") + 1
    keys: set[str] = set()

    for line in lines[start:]:
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([a-zA-Z0-9_-]+):", line)
        if match:
            keys.add(match.group(1))
    return keys


def test_cloud_jobs_are_manual_founder_only_and_depend_on_authorization() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert _mapping_keys(workflow, "on") == {"workflow_dispatch"}
    assert _mapping_keys(workflow, "jobs") == {
        "authorize-production",
        "build-publish",
        "deploy",
    }
    assert "\npermissions: {}\n" in workflow
    workflow_scope = workflow.split("\njobs:\n", maxsplit=1)[0]
    assert "${{ secrets." not in workflow_scope
    assert "${{ vars." not in workflow_scope

    authorization = _job_block(workflow, "authorize-production")
    assert "permissions: {}" in authorization
    assert "${{ secrets." not in authorization
    assert "${{ vars." not in authorization
    assert "azure/login" not in authorization
    assert "docker push" not in authorization
    assert "az containerapp" not in authorization

    for name in ("build-publish", "deploy"):
        job = _job_block(workflow, name)
        for required in (
            "github.event_name == 'workflow_dispatch'",
            "github.ref == 'refs/heads/main'",
            "github.actor == 'JohnClem'",
            "github.triggering_actor == 'JohnClem'",
            "needs.authorize-production.result == 'success'",
            "bash .github/scripts/verify-cab-release.sh",
        ):
            assert required in job

    assert "needs: authorize-production" in _job_block(workflow, "build-publish")
    assert "needs: [authorize-production, build-publish]" in _job_block(
        workflow, "deploy"
    )


def test_release_verification_and_mutation_rechecks_fail_closed() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    verifier = VERIFIER.read_text(encoding="utf-8")

    for required in (
        'test "$EVENT_NAME" = "workflow_dispatch"',
        'test "$WORKFLOW_REF" = "refs/heads/main"',
        'test "$ACTOR" = "JohnClem"',
        'test "$TRIGGERING_ACTOR" = "JohnClem"',
        'test "$EXPECTED_WORKFLOW_SHA" = "$RELEASE_SHA"',
        "git/ref/heads/main",
        "Independent agent review: PASS",
        "Reviewed commit: $RELEASE_SHA",
        "Reviewer agent:",
    ):
        assert required in verifier

    assert workflow.count("git/ref/heads/main") == 4
    assert "Reverify current main before requesting cloud credentials" in workflow
    assert "Reverify current main, then publish" in workflow
    assert "Reverify current main before migration mutation" in workflow
    assert "Reverify current main before application mutation" in workflow


def test_every_external_action_is_pinned_to_a_commit() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    action_refs = re.findall(r"^\s*uses: ([^\s#]+)", workflow, flags=re.MULTILINE)

    assert action_refs
    for action_ref in action_refs:
        _, separator, revision = action_ref.rpartition("@")
        assert separator, f"action is not pinned: {action_ref}"
        assert re.fullmatch(r"[0-9a-f]{40}", revision), (
            f"action must use a full commit SHA: {action_ref}"
        )
