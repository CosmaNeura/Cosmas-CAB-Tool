# Activate the SMS-only staff API without an unused upload route — Tier 2

Base: `e63ac63f5da9e2cfa6f7a28e6fde65865dd7d4d4`.

## Outcome and boundaries

A fully configured Twilio SMS-only staff API currently fails startup on the
default `COSMANEURA_UPLOAD_URL`, although its SMS dispatch path never consumes
that setting. Permit this topology to start without fabricating a working
upload endpoint. The separate Twilio worker already has no upload-route setting.

The exception applies only when the configured transport is Twilio and the
normalized enabled channels are exactly `sms`. Retain the existing upload-route
validation for ACS email and voice-enabled configurations. Keep authentication,
durable stores, explicit operational activation, scheduling, the secret gate
key, consent, staff authorization, direct-contract checks, suppression, quiet
hours, touch limits, and the recipient allowlist unchanged. The API remains
transport-inert; this change does not send a message or enable any deployment.

## Risk and dependencies

Tier 2 because settings govern outreach. Discovery F3/F9 and `CLAUDE.md` retain
their fail-closed direct-contract and synthetic-data boundaries. Current SMS
templates ask for contact/reply and do not render the upload setting; custom
staff text continues through its existing review gates. The legacy upload-link
sequence is forced to record-only by `DeliveryConfiguration.from_settings` when
Twilio is selected. This exception must be revisited if SMS later consumes the
configured upload route. No cross-repo contract, external dependency, schema, or
data change is required.

## Current deployed state and release

This local patch does not reconcile or replace the deployed application. The
parent SMS activation work owns current image/configuration/schema reconciliation
and any rollout. Build and promote only a reviewed, merged source artifact;
retain the existing live artifact until that process completes. No deployment,
messaging, or PHI authorization is inferred from these tests.

All communications defaults remain dark. An operator must still separately set
the existing communications/transport and activation switches. The SMS trial must
also retain explicit test mode and approved recipient scope. No additional
release flag is needed for this configuration correction.

## Local acceptance

Reproduce the current failure using fully configured synthetic staging settings.
Then exercise actual Settings-to-dispatch composition for staging and production:
no upload URL is supplied, the staff dispatcher has no outbound adapter, the
separate worker has the same activated policy, and the legacy upload/email lane
remains record-only. Verify that disabled activation, missing durable stores,
disabled auth, short gate keys, incomplete Twilio metadata, and ACS/voice upload
route requirements still fail closed. Run the existing focused messaging
settings/recipient-boundary checks plus the unchanged anti-upcode proof and fuzz
tests. No vendor, credential, or live database access is needed.

## Recovery and completion

Recover by disabling the existing patient-messaging/scheduler activation latches
and reverting this source slice; do not erase immutable outreach evidence. There
is no migration, backfill, infrastructure rollback, or destructive cleanup.
Local completion requires the regression and guard checks to pass, a concise
diff, and independent review of the final source before commit/promotion. Hosted
operational testing and carrier registration remain separate parent work.
