# Azure OpenAI pilot cost-plan addendum

Prepared: 2026-08-18

Region: North Central US

Subscription: Microsoft Azure Sponsorship

Resource group: `cosmaneura-cab-pilot`

Approval state: **APPROVED AND PROVISIONED — APPLICATION INFERENCE DISABLED**

This addendum preserves the approved 2026-08-18 platform plan and prices the
replacement of external Anthropic inference with a dedicated, privately
networked Azure OpenAI deployment. The account and model are now provisioned;
the pilot remains inference-off and contains no Anthropic credential. See the
[2026-08-19 evidence record](2026-08-19-demo-release-candidate-evidence.md).

## Provisioned inference boundary

- Dedicated `Microsoft.CognitiveServices/accounts` resource, kind `OpenAI`
- Local authentication disabled; no API key or Key Vault model credential
- Public network access disabled; network default action `Deny`
- One private endpoint and `privatelink.openai.azure.com` private DNS zone
- Existing API user-assigned managed identity receives the application-specific,
  account-scoped `Cognitive Services OpenAI User` role
- Migration and chase-worker identities receive no Azure OpenAI role or config
- `gpt-5.6-sol`, version `2026-07-09`, `DataZoneStandard`, capacity 200,
  `NoAutoUpgrade`
- Application inference is explicitly disabled outside bounded synthetic acceptance
- The dormant direct `api.openai.com` embedding seam remains prohibited in
  staging/production and contributes no pilot usage or cost

North Central US catalog and ARM validation confirmed this exact account,
model, version, SKU, and capacity shape on 2026-08-18. The model is GA,
supports the Responses API, and has a published retirement date of 2028-01-11.
GlobalStandard quota is fully allocated in the subscription; DataZoneStandard
has 3,333 available capacity units. Capacity 200 is a quota allocation, not a
fixed provisioned-throughput charge.

Catalog availability is not model acceptance. A bounded synthetic run on image
`1c5d7e7` exercised strict `text.format` JSON Schema output and the locally
selected JSON-object compatibility path through data-plane API
`2025-04-01-preview`; its closed-schema LLM events passed the canary scan and the
application was returned to inference-off. The 2026-08-19 release-candidate
baseline `de2b16f` was not deployed; the eventual merged manifest digest must
repeat the repository-owned acceptance harness. A provider 4xx
remains terminal and must not trigger a weaker retry.

## Revised recurring estimate

| Cost driver | Monthly USD |
|---|---:|
| Previously approved standing platform | $224.06 |
| Additional Azure OpenAI private endpoint | $7.30 |
| Additional private DNS zone | $0.50 |
| Azure OpenAI account/model deployment while idle | $0.00 |
| **Revised standing subtotal before tokens** | **$231.86** |

The private-endpoint line is the hourly standing charge: `$0.01/hour × 730`
= `$7.30/month`. Azure Private Link data processing is a separate per-GB meter
and is not part of the exact `$7.80/month` fixed network increase. It is expected
to be negligible at pilot traffic but must remain visible as variable usage.

The revised standing range is therefore about **$232–$248/month** at very low
model usage, before subscription credits, log volume, egress, or patient
messaging. The prior $300 review threshold is not an enforced Azure budget.

## Token pricing and scenarios

North Central US DataZoneStandard public retail rates used for the proposed
model:

- uncached input: **$5.50 per million tokens**
- cached input: **$0.55 per million tokens**
- output: **$33.00 per million tokens**

No production chart-volume or measured-token baseline exists yet. The table
below is illustrative only, using 12,000 aggregate uncached input tokens and
2,000 output tokens per encounter across the engine's two extraction calls.
That assumption costs $0.132 per encounter.

| Encounters/month | Model tokens | Platform + model |
|---:|---:|---:|
| 100 | $13.20 | $245.06 |
| 500 | $66.00 | $297.86 |
| 1,000 | $132.00 | $363.86 |

The existing 300–500 held-claim figure is not total encounter volume and must
not be used as a forecast. The next repository-owned synthetic acceptance run
must preserve the actual input, cached-input, and output token counters as safe
aggregates. A budget forecast should then be recalculated from measured p50/p95
use and expected monthly encounters.

## Lower-cost alternatives are not accuracy-approved

| North Central US DataZone model | Input/M | Output/M | Status |
|---|---:|---:|---|
| GPT-5.6 Sol | $5.50 | $33.00 | Synthetic data-plane accepted; accuracy evaluation pending |
| GPT-5.6 Terra | $2.75 | $16.50 | Requires clinical extraction evaluation |
| GPT-5.6 Luna | $1.10 | $6.60 | Requires clinical extraction evaluation |
| GPT-5.4 mini | $0.825 | $4.95 | Not accepted for this deployment |
| GPT-4o 2024-11-20 | $2.75 | $11.00 | Legacy; retires 2027-04-14 |

The initial proposal does not downshift solely for cost. Model substitution can
change extracted facts and therefore must pass the corpus, evidence, latency,
and guardrail acceptance suite. Deterministic Python remains the only coding
adjudicator; the model never raises an E/M level or adds a code directly.

## Approval boundary

Approval of this addendum authorized creation of the private Azure OpenAI
account, model deployment, private endpoint, DNS zone, and API-identity
application role assignment. It does **not** authorize real patient data or
application inference enablement.

The signed-in human operator currently inherits a subscription-level Foundry
User role. It is outside the application assignment but remains a
minimum-necessary entitlement review item before any real-PHI decision.

Real chart text remains prohibited until the Microsoft agreement/BAA is
verified to cover the selected Azure OpenAI service and US data zone, the
service data-processing/retention posture is documented, private-network and
managed-identity acceptance is green, and the P5 log scanner passes. Parameter
enablement or successful resource deployment is not evidence that those gates
are complete.

Executed decision and next control point:

1. The exact **$7.80/month fixed increase**, plus model-token usage and Azure
   Private Link per-GB data processing, was approved.
2. The dedicated private resource was provisioned and left inference-disabled.
3. Synthetic structured-output and telemetry acceptance passed on image
   `1c5d7e7`; repeat it for the eventual merged release manifest digest.
4. Reprice from measured p50/p95 tokens before any broader pilot volume.
