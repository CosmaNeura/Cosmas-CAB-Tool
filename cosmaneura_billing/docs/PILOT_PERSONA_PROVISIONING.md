# Pilot persona provisioning and acceptance

Status: tooling and acceptance steps are ready. Live assignments require the
operator-provided immutable Microsoft Entra object IDs and are not performed by
application deployment.

## Exact persona matrix

| Login | Entra + database roles | Visible tabs | Card Chase mutations |
|---|---|---|---|
| Provider | `Provider` | Provider only | No |
| Medical coder | `Coder` | Provider, Coder review, Card Chase | No |
| Card Chase staff | `BillingStaff` | Card Chase only | Yes |
| Administrator | `Admin` | All three | Yes, including admin-only gates |

The API computes navigation and `capabilities.manageChase`; the browser never
interprets role names. A Coder who also needs to work Card Chase must be
explicitly assigned both `Coder` and `BillingStaff`. A Provider mistakenly
assigned `BillingStaff` remains restricted to the Provider tab and cannot call
Card Chase routes.

## Provision one reviewed identity

Prerequisites:

- Entra enterprise application has **Assignment required = Yes** and exactly
  `Provider`, `Coder`, `BillingStaff`, and `Admin` application roles.
- The operator has the user's immutable Entra object ID (`oid`), not an email,
  display name, invitation ID, or group label.
- `DATABASE_URL`, `COSMANEURA_ENTRA_TENANT_ID`, and
  `COSMANEURA_PRACTICE_ID` identify the migration-owner boundary for the one
  intended pilot practice.
- The same role assignment has already been reviewed in Entra. Effective
  authorization is the intersection of the Entra token role and the active
  PostgreSQL membership.

From a private operator terminal, run exactly one reviewed command per login:

```bash
python -m cosmaneura_billing.memberships <provider-oid> --role Provider
python -m cosmaneura_billing.memberships <coder-oid> --role Coder
python -m cosmaneura_billing.memberships <card-chase-oid> --role BillingStaff
```

Do not place object IDs or database connection strings in tickets, shell
history shared with others, screenshots, source control, or this document.
Provisioning prints only `membership updated`.

For deliberate combined coder/staff access:

```bash
python -m cosmaneura_billing.memberships <reviewed-oid> \
  --role Coder --role BillingStaff
```

Disable access in both Entra and PostgreSQL during offboarding:

```bash
python -m cosmaneura_billing.memberships <reviewed-oid> \
  --role <current-role> --disable
```

Database disablement is immediate for new requests; revoke active Entra
sessions according to the tenant runbook as a separate operator action.

## Live acceptance

Use four dedicated synthetic-pilot accounts. In a private browser session for
each account, inspect `GET /api/session` and the rendered navigation:

1. Provider: `views=["provider"]`, `manageChase=false`; coder and every chase
   route return 403.
2. Coder: all three views, `manageChase=false`; the worklist opens, but the UI
   is read-only and `POST /api/chase/action` returns 403.
3. Card Chase staff: `views=["chase"]`, `manageChase=true`; provider/coder
   routes return 403. Against the fixed synthetic pack, assign to self, record
   a manual phone attempt, mark submitted, verify, refresh, and confirm the
   closed state persists.
4. Admin: all three views, `manageChase=true`, and the separately advertised
   digest-pinned synthetic seed action. Admin-only policy/release routes remain
   unavailable to every other persona.
5. Reuse one exact Card Chase action UUID with the same payload and confirm no
   second attempt is recorded. Reuse it with a different payload and confirm a
   409 response.
6. Confirm every synthetic action leaves scheduler, patient messaging,
   operational activation, and transport in the documented dormant
   `record_only` state. No call, SMS, email, link opening, or Athena write may
   occur.

Record only pass/fail, HTTP status, build digest, test account label, and UTC
time. Do not record tokens, headers, object IDs, chase IDs, patient labels,
destinations, notes, or response bodies in the acceptance artifact.

## Rollback

If any persona sees an extra tab or mutation control:

1. Disable that database membership and remove the Entra assignment.
2. Keep the application revision online only if the remaining personas are
   unaffected; otherwise restore the prior immutable image digest.
3. Leave patient messaging and Athena sync disabled.
4. Record the fixed failure code and affected persona, without identity or
   patient data, then repeat all four acceptance cases after correction.
