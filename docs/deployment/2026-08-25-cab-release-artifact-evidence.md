# CAB retained release-artifact evidence

Recorded: 2026-08-25 EDT (workflow completed 2026-08-26 UTC)

Scope: build and retain a reviewed image archive only. This record does not
authorize or claim registry publication, database migration, Azure mutation,
promotion, deployment, live acceptance, or real-PHI readiness.

## Bound identities

| Boundary | Identity |
|---|---|
| Reviewed and current `main` commit | `f490e6576aaddc9e217e54e7a04d6c8737e7c96d` |
| Independent post-merge review | [PR #22 comment](https://github.com/CosmaNeura/Cosmas-CAB-Tool/pull/22#issuecomment-5419926682) |
| Artifact workflow | [run 32924493939](https://github.com/CosmaNeura/Cosmas-CAB-Tool/actions/runs/32924493939) |
| Retained artifact | [artifact 9591010570](https://github.com/CosmaNeura/Cosmas-CAB-Tool/actions/runs/32924493939/artifacts/9591010570) |
| GitHub artifact digest | `sha256:6ea693248a53e858ece05bb82620b869aa0b68a5205588873252c5ddd0af0569` |
| Image ID | `sha256:17c57252938a03b888ed63af24065013a673a01a3fc896693d9ed420f94ea545` |
| Image archive SHA-256 | `6722ea3abef1ba3e1db625ae9139a9cad2f5f7f189533e2ea566007f3814b652` |
| Migration manifest SHA-256 | `37065d9f8a0299fbc0251c1ca718d7b30bb11d9d3da4986dd453407767763b70` |
| Migration files | 18 |
| GitHub expiration | 2026-11-24 |

The workflow ran from exact current `main`, validated the merged independent
review record before checkout, built the image without cloud credentials,
rechecked `main` before retention, and uploaded exactly `image.tar.gz`,
`migrations.sha256`, and `release-metadata.json`.

Downloaded verification confirmed that the metadata names the reviewed source,
image, review record, and file digests; the gzip archive is valid; the archive's
unique regular config member hashes to the image ID and carries the source
revision label; and all 18 migration hashes match the exact source commit.
GitHub reports zero deployment records for the source commit.

The earlier [run 32922884780](https://github.com/CosmaNeura/Cosmas-CAB-Tool/actions/runs/32922884780)
failed closed before retention because Docker/BuildKit emitted an OCI config
path. PR #22 added strict support for that path while rejecting traversal,
nested aliases, symlinks, hardlinks, and duplicate members. The failed run
created no artifact or deployment.

## Next gate

The existing `Build & Deploy` workflow does not consume this artifact. Before
any promotion, separately reconcile the retained source/image with the live
digest, configuration, schema, and Azure state, then obtain explicit production
authorization. Until artifact consumption is implemented and reviewed, a
successful retained build is evidence of buildability—not deployment.
