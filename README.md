# CosmaNeura Computer-Assisted Billing (CAB)

> **Current source of truth (2026-08-19):** the maintained billing application
> lives under [`cosmaneura_billing/`](cosmaneura_billing/). GitHub `main` is the
> code source of truth; the accepted Azure image may lag it. See the
> [deployment source-of-truth map](docs/deployment/README.md) and the
> [current synthetic-only evidence record](docs/deployment/2026-08-19-demo-release-candidate-evidence.md).
> The pilot remains private with inference, scheduling, and messaging disabled;
> real-PHI readiness has not been established.
>
> **Current product boundary (2026-08-27):** the maintained CAB application is
> advisory billing intelligence. It does not generate or transmit 837P, submit
> claims, enroll with a clearinghouse or payer, or write to an EHR. Public
> signup and onboarding do not request or require NPI. The separately governed,
> default-off Card Chase outreach workflow retains its existing gates. See
> [`NPI_AND_CLAIM_SUBMISSION_SCOPE.md`](cosmaneura_billing/docs/NPI_AND_CLAIM_SUBMISSION_SCOPE.md).
> Provider credential/licensure status is informational only and has no effect on
> signup, trial, billing, continued use, entitlement, CAB capability, or real-PHI
> access. Practices authorize workforce membership and roles; CosmaNeura does not
> claim to verify professional status as a condition of use.

This repository contains the private CosmaNeura Computer-Assisted Billing (CAB)
pilot. The maintained application and its documentation live in
[`cosmaneura_billing/`](cosmaneura_billing/).

CAB is not the Cosmas clinical-notes product and is not the 2023 FluencyMed
prototype. Historical FluencyMed code and datasets that remain committed,
including CCSR and iCliniq material, are leftovers from earlier work. Their
presence is not a claim about CAB's current product, capabilities, evidence
base, availability, or traction.

## Start here

- [CAB application and developer documentation](cosmaneura_billing/README.md)
- [Deployment and source-of-truth map](docs/deployment/README.md)
- [NPI and claim-submission scope](cosmaneura_billing/docs/NPI_AND_CLAIM_SUBMISSION_SCOPE.md)
