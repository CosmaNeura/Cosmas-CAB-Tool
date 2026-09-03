# Card Chase worker observability spec

How to monitor and alert on the scheduled Card Chase worker
(`python -m cosmaneura_billing.chase.worker --once`) without crossing the PHI
logging boundary. This is a spec for the operator/deployment layer; it changes
no code. It reflects the worker's actual emission contract in
`cosmaneura_billing/chase/worker.py` and `runtime.py`.

## The emission contract (what you have to work with)

The worker's **entire** operational logging contract is **one PHI-free terminal
event per invocation**, emitted by `_emit_run_result`:

- **logger:** `cosmaneura_billing.chase.worker`
- **event:** `chase_worker_run` (`CHASE_WORKER_LOG_EVENT`)
- **level:** `info` when the run is `completed`, otherwise `error`
- **fields (`extra`):** `status`, `quarantined`, `ambiguous`, `scheduled`,
  `reserved`, `claimed`, `accepted`, `permanent_failures`

Per-delivery diagnostics are **deliberately discarded** at the PHI boundary
(`ChaseWorker._log` is a no-op): a durable delivery UUID is pseudonymous patient
data because it joins back to the outbox. **All alerting must therefore key off
the terminal aggregate event and the job exit code — never per-delivery logs.**

### Statuses and exit codes

| `status` | exit | Meaning | Outreach effect |
|---|---|---|---|
| `completed` | `0` | Bounded pass finished cleanly | Normal |
| `configuration_error` | `2` | Job could not start safely (config/DSN/secret/policy) | Nothing ran |
| `transport_uncertain` | `1` | Ambiguous/quarantined delivery, lease expiry, or an uncertain provider result | **New outreach halted** |
| `database_uncertain` | `1` | A repository operation's result is not provable | **New outreach halted** |

### Two safety behaviors monitoring must respect

- **Ambiguity halt:** if any `quarantined` or `ambiguous` deliveries exist, the
  run returns `transport_uncertain` (exit `1`) **before** claiming or dispatching
  anything. An ambiguous delivery is investigated and reconciled by a human; it
  is **never** blindly retried.
- **`record_only` / latch-off:** the run stops after reserving due work with
  `status=completed`; it may have created durable **staff phone tasks** but makes
  no transport claim. `accepted` will be `0` in this mode — that is healthy, not
  a failure.

## Signals and alert rules

| # | Condition (over the `chase_worker_run` event / exit code) | Severity | Action |
|---|---|---|---|
| 1 | **No `chase_worker_run` event** within the expected schedule window (heartbeat) | page | Job not firing or crashed before emit — check Container Apps job execution history |
| 2 | `status = configuration_error` (exit `2`) | page | Misconfiguration/secret/DSN/policy; the job cannot start — safe (nothing sent) but broken |
| 3 | `status in (transport_uncertain, database_uncertain)` (exit `1`) | page | New outreach halted; investigate; **do not** blind-retry |
| 4 | `ambiguous > 0` **or** `quarantined > 0` | page | Ambiguous lane needs human reconciliation before outreach resumes |
| 5 | `permanent_failures` elevated vs baseline | ticket | Template/gate/roster rejections; inspect config and roster, not patients |
| 6 | `accepted` stays `0` across consecutive runs **while messaging is activated** | ticket | Cadence not producing sends; check policy activation and due-work reservation |
| 7 | Container Apps job `exit code != 0` (independent of the event) | page | Backstop for cases where the event never emitted |

Heartbeat (#1) is the most important positive check: a worker that never runs
sends nothing and raises no error on its own.

## Implementation guidance (Azure Monitor / Log Analytics)

Query the structured JSON logs; the fields above are top-level on the
`chase_worker_run` event. Sketches (adapt table/column names to the workspace):

Last run and status:

```kusto
ContainerAppConsoleLogs_CL
| where Log_s has "chase_worker_run"
| extend p = parse_json(Log_s)
| where p.event == "chase_worker_run"
| project TimeGenerated, status=tostring(p.status), ambiguous=toint(p.ambiguous),
          quarantined=toint(p.quarantined), accepted=toint(p.accepted),
          permanent_failures=toint(p.permanent_failures)
| order by TimeGenerated desc
```

Heartbeat alert (no successful run in the window). Count completed runs so the
query returns a row (`completed == 0`) when the job never ran, crashed before
emitting, or only produced error statuses — a `max(TimeGenerated)`/`where` form
returns zero rows in exactly those cases and the alert would never fire. Set the
Azure Monitor rule to fire when the result count is greater than 0, evaluated
over one schedule interval plus a margin:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(<one schedule interval + margin>)
| where Log_s has "chase_worker_run"
| extend p = parse_json(Log_s)
| summarize completed = countif(tostring(p.status) == "completed")
| where completed == 0
```

Halt/uncertainty alert:

```kusto
ContainerAppConsoleLogs_CL
| where Log_s has "chase_worker_run"
| extend p = parse_json(Log_s)
| where p.status in ("transport_uncertain","database_uncertain")
       or toint(p.ambiguous) > 0 or toint(p.quarantined) > 0
```

Prefer alerting on the Container Apps **job execution exit code** where
available as a second, log-independent signal (rule #7).

## Dashboard (suggested tiles)

- Time since last `status=completed` run (heartbeat).
- Runs by `status` over time.
- `accepted` per day (0 is expected in `record_only`).
- `ambiguous` + `quarantined` (target: 0; any nonzero is a halt).
- `permanent_failures` trend.

## Privacy constraints (do not regress)

- Never add per-delivery logging to gain visibility — it would cross the PHI
  boundary the worker deliberately holds. Aggregate counts + exit code are the
  contract.
- Do not emit or store delivery UUIDs, recipients, patient references, or
  message content in alerts, dashboards, or tickets. Record only fixed
  status/error codes, counts, execution names, image digest, and the UTC window.
- The deployment PHI-safe log scan (`log_scanner`) already covers the
  `chase-worker-job-console` stream via `is_safe_chase_worker_event`; keep that
  stream in the deployment count-report and require a nonzero row count with zero
  canary matches before enabling outreach.

## Cross-references

- Rollback + ambiguous-delivery handling: `cosmaneura_billing/docs/PILOT_OPERATIONS_RUNBOOK.md`.
- Worker/scheduler least-privilege split and logging boundary: `cosmaneura_billing/docs/ACCESS_CONTROL.md`.
- The email-only activation cascade, go/no-go, and rollback are covered by the
  Card Chase email-only Beta activation flight plan (proposed separately under
  `docs/flight-plans/`).

## Open items (owner: deployment/platform)

- Confirm the scheduled job cadence and set the heartbeat window accordingly.
- Wire rules #1–#7 as Azure Monitor alerts against the pilot workspace.
- Decide `permanent_failures` and `accepted=0`-while-activated thresholds from
  the observed distribution of the `chase_worker_run` counters after the first
  week of runs — these are worker operational metrics, distinct from the
  worklist/card baseline used by `chase/metrics.py::baseline_comparison`.
