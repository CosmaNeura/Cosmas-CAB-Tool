#!/usr/bin/env bash

set -euo pipefail

: "${ACTOR:?ACTOR is required}"
: "${EVENT_NAME:?EVENT_NAME is required}"
: "${EXPECTED_WORKFLOW_SHA:?EXPECTED_WORKFLOW_SHA is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"
: "${REVIEW_URL:?REVIEW_URL is required}"
: "${TRIGGERING_ACTOR:?TRIGGERING_ACTOR is required}"
: "${WORKFLOW_REF:?WORKFLOW_REF is required}"

test "$EVENT_NAME" = "workflow_dispatch"
test "$GITHUB_REPOSITORY" = "CosmaNeura/Cosmas-CAB-Tool"
test "$WORKFLOW_REF" = "refs/heads/main"
test "$ACTOR" = "JohnClem"
test "$TRIGGERING_ACTOR" = "JohnClem"

if [[ ! "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "release_sha must be a full lowercase commit SHA" >&2
  exit 1
fi

test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test "$EXPECTED_WORKFLOW_SHA" = "$RELEASE_SHA"

current_main="$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq '.object.sha')"
test "$current_main" = "$RELEASE_SHA"

review_pattern='^https://github\.com/CosmaNeura/Cosmas-CAB-Tool/pull/([0-9]+)#issuecomment-([0-9]+)$'
if [[ ! "$REVIEW_URL" =~ $review_pattern ]]; then
  echo "agent_review_url must be a Cosmas-CAB-Tool PR comment URL" >&2
  exit 1
fi
pull_number="${BASH_REMATCH[1]}"
comment_id="${BASH_REMATCH[2]}"

mapfile -t pull_record < <(
  gh api "repos/$GITHUB_REPOSITORY/pulls/$pull_number" \
    --jq '[.base.ref, (.merged_at // ""), (.merge_commit_sha // "")] | .[]'
)
test "${pull_record[0]:-}" = "main"
test -n "${pull_record[1]:-}"
test "${pull_record[2]:-}" = "$RELEASE_SHA"

review_record="$(gh api "repos/$GITHUB_REPOSITORY/issues/comments/$comment_id")"
test "$(jq -r '.html_url' <<<"$review_record")" = "$REVIEW_URL"
test "$(jq -r '.issue_url' <<<"$review_record")" = \
  "https://api.github.com/repos/$GITHUB_REPOSITORY/issues/$pull_number"

review_body="$(jq -r '.body' <<<"$review_record")"
grep -Fqx "Independent agent review: PASS" <<<"$review_body"
grep -Fqx "Reviewed commit: $RELEASE_SHA" <<<"$review_body"
grep -Eq '^Reviewer agent: [^[:space:]].+$' <<<"$review_body"
