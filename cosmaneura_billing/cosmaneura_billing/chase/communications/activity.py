"""Read-only dispatch activity. A start is an attempt, never proof of delivery.

Only dispatch starts create entries; receipts, callbacks and staff reviews can
describe an entry but cannot multiply it. No transport or mutation is available.
"""
from ..activity import CHANNELS, HISTORY_LIMIT, SCHEMA_VERSION
from .postgres import _iso


class CommunicationsActivity:
    def __init__(self, store):
        self.store = store

    def counts(self, case_ids, *, db=None):
        """Bound each query to 100 already-authorized worklist cases, not N+1."""
        ids = list(dict.fromkeys(case_ids))
        if not ids:
            return {}
        if db is None:
            with self.store._db() as connection:
                return self.counts(ids, db=connection)
        result = dict.fromkeys(ids, 0)
        for offset in range(0, len(ids), 100):
            rows = db.execute("""SELECT o.case_id,count(*) AS total
                FROM chase_communication_dispatch_starts d
                JOIN chase_communication_operations o USING(practice_id,operation_id)
                WHERE d.practice_id=%s AND o.case_id=ANY(%s::text[]) GROUP BY o.case_id""",
                (self.store.practice_id, ids[offset:offset+100])).fetchall()
            result.update({row["case_id"]: row["total"] for row in rows})
        return result

    def read(self, case_id):
        with self.store._db() as db:
            self.store._case(db, case_id)
            # Window totals and the history share one statement snapshot. Select
            # the latest starts before joining callbacks; callback volume cannot
            # truncate attempts or inflate their count.
            rows = db.execute("""WITH recent AS (
                SELECT o.operation_id,o.channel,o.binding_id,d.started_at,
                    count(*) OVER () AS total,
                    count(*) FILTER (WHERE o.channel='sms') OVER () AS sms,
                    count(*) FILTER (WHERE o.channel='automated_voice') OVER () AS phone
                FROM chase_communication_dispatch_starts d
                JOIN chase_communication_operations o USING(practice_id,operation_id)
                WHERE d.practice_id=%s AND o.case_id=%s
                ORDER BY d.started_at DESC,o.operation_id DESC LIMIT %s
            ) SELECT recent.channel,recent.started_at,recent.total,recent.sms,recent.phone,
                CASE WHEN r.outcome='accepted' THEN state.status
                     WHEN r.outcome='rejected' THEN 'rejected'
                     ELSE COALESCE(u.decision,'unknown') END AS outcome
            FROM recent
            JOIN chase_communication_bindings b ON b.practice_id=%s AND b.binding_id=recent.binding_id
            LEFT JOIN chase_communication_dispatch_receipts r ON r.practice_id=b.practice_id
                AND r.operation_id=recent.operation_id
            LEFT JOIN chase_communication_unknown_resolutions u ON u.practice_id=b.practice_id
                AND u.operation_id=recent.operation_id
            LEFT JOIN LATERAL (
                SELECT status FROM (
                    SELECT r.status,0::bigint AS sequence_no
                    UNION ALL
                    SELECT e.status,e.sequence_no FROM chase_communication_provider_events e
                    WHERE e.practice_id=r.practice_id AND e.account_sid=r.account_sid
                      AND e.provider_sid=r.provider_sid AND e.kind IN ('sms_status','voice_status')
                      AND (e.sender IS NULL OR e.sender=b.sender) AND (e.recipient IS NULL OR e.recipient=b.phone)
                ) choices
                ORDER BY CASE
                    WHEN status='read' THEN 6 WHEN status IN ('delivered','completed') THEN 5
                    WHEN status IN ('failed','undelivered','busy','no-answer','canceled') THEN 4
                    WHEN status IN ('sent','in-progress') THEN 3 WHEN status IN ('sending','ringing') THEN 2
                    WHEN status='initiated' OR (recent.channel='sms' AND status='queued') THEN 1 ELSE 0 END DESC,
                    sequence_no DESC LIMIT 1
            ) state ON r.outcome='accepted'
            ORDER BY recent.started_at DESC,recent.operation_id DESC""",
                (self.store.practice_id, case_id, HISTORY_LIMIT, self.store.practice_id)).fetchall()
        counts = dict.fromkeys(CHANNELS, 0)
        if rows:
            counts.update(sms=rows[0]["sms"], phone=rows[0]["phone"])
        history = [{"at": _iso(row["started_at"]),
                    "channel": "sms" if row["channel"] == "sms" else "phone",
                    "performer": "system", "outcome": row["outcome"], "minutes": 0}
                   for row in rows]
        total = rows[0]["total"] if rows else 0
        return {"schemaVersion": SCHEMA_VERSION, "totalAttempts": total, "countsByChannel": counts,
                "staffMinutes": 0, "lastAttempt": dict(history[0]) if history else None,
                "history": history, "truncated": total > HISTORY_LIMIT}
