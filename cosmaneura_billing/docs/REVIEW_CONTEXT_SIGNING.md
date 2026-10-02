# Review token signing

Coder review tokens use a server secret independent of encounter/source IDs.
The v3 MAC binds the purpose, recommendation payload, internal encounter ID,
encounter version, practice, tenant and reviewer. Knowing a source identifier
does not authorize a new recommendation manifest.

Before deploying this version, provision a cryptographically random secret of
at least 32 ASCII characters through the deployment secret store as
`COSMANEURA_REVIEW_CONTEXT_SIGNING_KEY`. Use the same value across API replicas.
The setting is redacted in configuration representations and is never returned
to the browser. A PostgreSQL or deployed application without it returns 503 for
coder review issuance/submission and does not report ready. No database migration
is required for this key. Only local development/test with local storage may
generate an ephemeral process key.

Old v2 tokens and tokens signed with a previous key are rejected. Users reload
the review to obtain a fresh token; stored feedback/history remains intact. A
local ephemeral-key restart similarly requires reload. Key rotation deliberately
invalidates outstanding tokens. Do not restore resource-ID-based signing as a
recovery path.

Compose requires this value in its explicit environment mapping; it has no
public default. The existing disposable CI topology supplies a test-only key,
which must never be copied to a deployed environment.
