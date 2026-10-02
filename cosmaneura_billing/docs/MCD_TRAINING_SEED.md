# Load the MCD synthetic staff-training cases

The fixed pack `cosmaneura-mcd-training-20260930-v1` contains twelve
Synthea-informed fictional visits, `SYN-CAB-20260930-01` through `12`. Visits
10–12 cover fertility/preconception. These are the September 30 source notes,
not the older `SYN-RC-*` demonstration pack. Clinical narratives and completion
are fictional; the pack contains no model answers or clinical approval.

After the reviewed application/migrations are installed, the database owner
runs this command from the application environment for the intended synthetic
practice:

```sh
python -m cosmaneura_billing.seed_mcd_training
```

The command takes no arguments and has no input-file option. It uses the
existing `DATABASE_URL`, `COSMANEURA_PRACTICE_ID` and `COSMANEURA_ENTRA_TENANT_ID` configuration.
Use the table-owning migration identity through the existing secret mechanism;
do not put credentials in the command, console output or a shared artifact.
The active practice must match the configured tenant. Migration 008's enabled
reserved-source guards must be present. Runtime/API credentials cannot seed.

The result contains only the pack ID, fixed digest, counts and a status. The
first complete insertion reports `inserted_count: 12`. An exact retry reports
`replayed_count: 12`, without changing existing source, versions or timestamps.
The expected seed digest is
`e62f6df781b8cba1b43eb8847e4e7e1f7b8f92a212ad49d2a48c953609c36594`.
If an ID already has different content, the entire insertion rolls back with
`conflict`; investigate the target rather than replacing or deleting its rows.
The older demo seed and this pack can coexist under their distinct IDs.

Sign in with an active same-practice `Coder` membership to select the cases in
the standard CAB queue. Seed loading does not grant user roles. This integration
supplies the protected source visits; editable billing headers and header
signoff are separate integrations. Existing billing and feedback capabilities
continue to use the configured full pipeline and their current access controls.
Do not treat packaging as approval of clinical output or model disclosure.
The command does not invoke a model, start outreach, import Athena data, or
enable a capability.

Confirm the twelve exact IDs in the authorized queue and retain the seed result
before training. Staff feedback remains separate from the immutable source
notes. Recovery preserves populated source and review history; restrict access
or disable the affected feature instead of deleting the pack. This runbook does
not authorize staging deployment or live-patient use.
