#!/usr/bin/env python3
"""Seal deterministic metadata around one CAB image release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
REVIEW_URL_PATTERN = re.compile(
    r"https://github\.com/CosmaNeura/Cosmas-CAB-Tool/pull/[0-9]+#issuecomment-[0-9]+"
)
MIGRATIONS_ROOT = "cosmaneura_billing/migrations"


def _git(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_image_archive(
    image_archive: Path,
    image_id: str,
    image_ref: str,
    source_sha: str,
) -> None:
    try:
        with tarfile.open(image_archive, mode="r:gz") as archive:
            manifest_file = archive.extractfile("manifest.json")
            if manifest_file is None:
                raise ValueError("manifest.json is not a file")
            manifest = json.load(manifest_file)
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise ValueError("archive must contain exactly one image manifest")

            image_manifest = manifest[0]
            config_path = image_manifest["Config"]
            repo_tags = image_manifest["RepoTags"]
            if not isinstance(config_path, str) or not isinstance(repo_tags, list):
                raise ValueError("image manifest has invalid Config or RepoTags")
            if repo_tags != [image_ref]:
                raise ValueError("image archive tag does not match the release reference")

            config_file = archive.extractfile(config_path)
            if config_file is None:
                raise ValueError("image config is not a file")
            config_bytes = config_file.read()
    except (KeyError, json.JSONDecodeError, tarfile.TarError, ValueError) as error:
        raise SystemExit(f"invalid image archive: {error}") from error

    config_digest = _sha256(config_bytes)
    if config_digest != image_id.removeprefix("sha256:"):
        raise SystemExit("image archive config does not match the image ID")
    if PurePosixPath(config_path).name != f"{config_digest}.json":
        raise SystemExit("image archive config path does not match the image ID")

    try:
        image_config = json.loads(config_bytes)
        revision = image_config["config"]["Labels"]["org.opencontainers.image.revision"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit("image archive has no source-revision label") from error
    if revision != source_sha:
        raise SystemExit("image archive source-revision label does not match the source SHA")


def _migration_paths(repository: Path, source_sha: str) -> list[str]:
    output = _git(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        source_sha,
        "--",
        MIGRATIONS_ROOT,
    )
    paths = [
        raw_path.decode("utf-8")
        for raw_path in output.split(b"\0")
        if raw_path and raw_path.endswith(b".sql")
    ]
    if not paths:
        raise SystemExit(f"no SQL migrations found under {MIGRATIONS_ROOT}")
    if any("\n" in path or "\r" in path for path in paths):
        raise SystemExit("migration paths containing newlines are unsupported")
    return sorted(paths)


def build_release(
    repository: Path,
    output_dir: Path,
    source_sha: str,
    image_archive: Path,
    image_id: str,
    image_migrations_dir: Path,
    image_ref: str,
    review_url: str,
) -> None:
    if SHA_PATTERN.fullmatch(source_sha) is None:
        raise SystemExit("source SHA must be a lowercase, full 40-character SHA")
    if IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise SystemExit("image ID must be a sha256 digest")
    if image_ref != f"cab-release:{source_sha}":
        raise SystemExit("image reference does not match the requested source SHA")
    if REVIEW_URL_PATTERN.fullmatch(review_url) is None:
        raise SystemExit("review URL must identify a CAB pull-request comment")
    if not image_archive.is_file():
        raise SystemExit("image archive does not exist")
    if not image_migrations_dir.is_dir():
        raise SystemExit("extracted image migrations directory does not exist")

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "image.tar.gz"
    if image_archive.resolve() != archive_path.resolve():
        raise SystemExit("image archive must be output as image.tar.gz in the release directory")
    _verify_image_archive(image_archive, image_id, image_ref, source_sha)

    resolved_sha = _git(repository, "rev-parse", f"{source_sha}^{{commit}}").decode().strip()
    if resolved_sha != source_sha:
        raise SystemExit("source SHA did not resolve to the exact requested commit")

    migration_paths = _migration_paths(repository, source_sha)
    image_paths = sorted(
        path.relative_to(image_migrations_dir).as_posix()
        for path in image_migrations_dir.rglob("*.sql")
        if path.is_file()
    )
    expected_image_paths = [
        path.removeprefix(f"{MIGRATIONS_ROOT}/") for path in migration_paths
    ]
    if image_paths != expected_image_paths:
        raise SystemExit("SQL migration set in the image does not match the source commit")

    migration_lines: list[str] = []
    for path, image_path in zip(migration_paths, image_paths, strict=True):
        migration = _git(repository, "show", f"{source_sha}:{path}")
        if (image_migrations_dir / image_path).read_bytes() != migration:
            raise SystemExit(f"image migration differs from source commit: {path}")
        migration_lines.append(f"{_sha256(migration)}  {path}\n")
    migration_manifest = "".join(migration_lines).encode("utf-8")

    manifest_path = output_dir / "migrations.sha256"
    manifest_path.write_bytes(migration_manifest)

    metadata = {
        "format": "cosmaneura-cab-image-release/v1",
        "image_archive": archive_path.name,
        "image_archive_sha256": _sha256_file(archive_path),
        "image_id": image_id,
        "image_ref": image_ref,
        "migration_file_count": len(migration_lines),
        "migrations_manifest": manifest_path.name,
        "migrations_manifest_sha256": _sha256(migration_manifest),
        "review_url": review_url,
        "source_sha": source_sha,
    }
    metadata_path = output_dir / "release-metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-archive", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--image-migrations-dir", type=Path, required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--review-url", required=True)
    args = parser.parse_args()
    build_release(
        args.repository.resolve(),
        args.output_dir.resolve(),
        args.source_sha,
        args.image_archive.resolve(),
        args.image_id,
        args.image_migrations_dir.resolve(),
        args.image_ref,
        args.review_url,
    )


if __name__ == "__main__":
    main()
