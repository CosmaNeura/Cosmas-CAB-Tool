from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/build_release_artifact.yml"
BUILDER = ROOT / ".github/scripts/build-cab-release-artifact.py"
REVIEW_URL = (
    "https://github.com/CosmaNeura/Cosmas-CAB-Tool/pull/21#issuecomment-123456"
)


def _run(*command: str, cwd: Path = ROOT, check: bool = True, **kwargs: object):
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def _step_blocks(workflow: str) -> list[tuple[str, str]]:
    starts = list(re.finditer(r"(?m)^      - name: (.+)$", workflow))
    return [
        (
            match.group(1),
            workflow[
                match.start() : starts[index + 1].start()
                if index + 1 < len(starts)
                else len(workflow)
            ],
        )
        for index, match in enumerate(starts)
    ]


def _run_block(step: str) -> str:
    match = re.search(
        r"(?ms)^        run: \|\n(?P<body>(?:          .*\n|\n)+)", step
    )
    assert match
    return "".join(
        f"{line[10:]}\n" if line else "\n"
        for line in match.group("body").splitlines()
    )


def _image_archive(path: Path, source_sha: str, image_ref: str) -> str:
    config = json.dumps(
        {"config": {"Labels": {"org.opencontainers.image.revision": source_sha}}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(config).hexdigest()
    manifest = json.dumps(
        [{"Config": f"{digest}.json", "Layers": [], "RepoTags": [image_ref]}],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    with path.open("wb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w|") as archive:
                for name, payload in (("manifest.json", manifest), (f"{digest}.json", config)):
                    member = tarfile.TarInfo(name)
                    member.mtime, member.size = 0, len(payload)
                    archive.addfile(member, io.BytesIO(payload))
    return f"sha256:{digest}"


def _build(output: Path, image_migrations: Path, source_sha: str, *, check: bool = True):
    output.mkdir()
    image_ref = f"cab-release:{source_sha}"
    archive = output / "image.tar.gz"
    image_id = _image_archive(archive, source_sha, image_ref)
    result = _run(
        "python3",
        str(BUILDER),
        "--repository",
        str(ROOT),
        "--output-dir",
        str(output),
        "--source-sha",
        source_sha,
        "--image-archive",
        str(archive),
        "--image-id",
        image_id,
        "--image-migrations-dir",
        str(image_migrations),
        "--image-ref",
        image_ref,
        "--review-url",
        REVIEW_URL,
        check=check,
    )
    return result, image_id


def test_sealer_is_deterministic_and_binds_image_migrations(tmp_path: Path) -> None:
    source_sha = _run("git", "rev-parse", "HEAD").stdout.strip()
    source_migrations = ROOT / "cosmaneura_billing/migrations"
    outputs = [tmp_path / "first", tmp_path / "second"]
    image_id = ""
    for output in outputs:
        extracted = tmp_path / f"{output.name}-migrations"
        shutil.copytree(source_migrations, extracted)
        _, image_id = _build(output, extracted, source_sha)

    for filename in ("image.tar.gz", "migrations.sha256", "release-metadata.json"):
        assert (outputs[0] / filename).read_bytes() == (outputs[1] / filename).read_bytes()

    manifest = (outputs[0] / "migrations.sha256").read_bytes()
    metadata = json.loads((outputs[0] / "release-metadata.json").read_text())
    assert metadata["source_sha"] == source_sha
    assert metadata["image_id"] == image_id
    assert metadata["image_archive_sha256"] == hashlib.sha256(
        (outputs[0] / "image.tar.gz").read_bytes()
    ).hexdigest()
    assert metadata["migrations_manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
    paths = [line.split("  ", 1)[1] for line in manifest.decode().splitlines()]
    assert paths == sorted(paths)
    assert paths == [
        path.relative_to(ROOT).as_posix() for path in sorted(source_migrations.glob("*.sql"))
    ]

    drifted = tmp_path / "drifted-migrations"
    shutil.copytree(source_migrations, drifted)
    drifted.joinpath(paths[0].rsplit("/", 1)[1]).write_text("SELECT 999;\n")
    rejected, _ = _build(tmp_path / "rejected", drifted, source_sha, check=False)
    assert rejected.returncode != 0
    assert "image migration differs from source commit" in rejected.stderr


def test_preflight_is_first_and_rejects_wrong_authority_or_evidence(tmp_path: Path) -> None:
    steps = _step_blocks(WORKFLOW.read_text())
    assert [name for name, _ in steps] == [
        "Trusted inline founder and current-main preflight",
        "Check out the verified release commit",
        "Verify checkout identity",
        "Build and seal image archive without cloud credentials",
        "Reverify current main before retention",
        "Retain immutable release files",
        "Record artifact identity",
    ]
    assert "uses:" not in steps[0][1]
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in steps[1][1]
    assert "ref: ${{ github.sha }}" in steps[1][1]
    assert "ref: ${{ inputs.release_sha }}" not in steps[1][1]

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gh").write_text(
        """#!/bin/sh
case "$*" in
  *git/ref/heads/main*) printf '%s\n' "$FAKE_MAIN_SHA" ;;
  *pulls/*) /usr/bin/jq -cn --arg sha "$FAKE_PULL_SHA" '{base:{ref:"main"},merged_at:"date",merge_commit_sha:$sha}' ;;
  *issues/comments/*) /usr/bin/jq -cn --arg url "$REVIEW_URL" --arg body "Independent agent review: PASS
Reviewed commit: $FAKE_REVIEW_SHA
Reviewer agent: /root/reviewer" '{html_url:$url,issue_url:"https://api.github.com/repos/CosmaNeura/Cosmas-CAB-Tool/issues/21",body:$body}' ;;
  *) exit 2 ;;
esac
"""
    )
    (fake_bin / "gh").chmod(0o755)

    sha = "a" * 40
    environment = {
        "ACTOR": "JohnClem",
        "CONFIRMATION": "BUILD CAB RELEASE ARTIFACT",
        "EVENT_NAME": "workflow_dispatch",
        "EXPECTED_WORKFLOW_SHA": sha,
        "FAKE_MAIN_SHA": sha,
        "FAKE_PULL_SHA": sha,
        "FAKE_REVIEW_SHA": sha,
        "GH_TOKEN": "synthetic",
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "RELEASE_SHA": sha,
        "REPOSITORY": "CosmaNeura/Cosmas-CAB-Tool",
        "REVIEW_URL": REVIEW_URL,
        "TRIGGERING_ACTOR": "JohnClem",
        "WORKFLOW_REF": "refs/heads/main",
    }
    preflight = _run_block(steps[0][1])
    assert subprocess.run(["bash", "-c", preflight], env=environment).returncode == 0
    for key in ("ACTOR", "TRIGGERING_ACTOR", "FAKE_MAIN_SHA", "FAKE_PULL_SHA", "FAKE_REVIEW_SHA"):
        rejected = environment | {key: "b" * 40}
        assert subprocess.run(
            ["bash", "-c", preflight], env=rejected, capture_output=True
        ).returncode != 0


def test_workflow_cannot_publish_or_deploy() -> None:
    workflow = WORKFLOW.read_text()
    action_refs = re.findall(r"(?m)^        uses: ([^\s#]+)", workflow)
    assert action_refs == [
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    ]
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs)
    assert workflow.count("contents: read") == 2
    assert workflow.count("issues: read") == 2
    assert workflow.count("pull-requests: read") == 2
    for forbidden in ("id-token:", "${{ secrets.", "azure/", "docker push", "az ", "DATABASE_URL"):
        assert forbidden not in workflow
    assert "docker build" in workflow and "docker save" in workflow
    upload = dict(
        re.findall(
            r"(?m)^          ([a-z0-9-]+): ([^|\n]+)$",
            dict(_step_blocks(workflow))["Retain immutable release files"],
        )
    )
    assert upload == {
        "name": "cab-release-${{ github.sha }}",
        "if-no-files-found": "error",
        "retention-days": "90",
        "compression-level": "0",
        "overwrite": "false",
    }
