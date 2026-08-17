-- 002_chase_persistence.up.sql
-- Durable, practice-scoped storage for the insurance-data chase.
-- PostgreSQL 14+; independent of pgvector and safe to run before 001.

BEGIN;

CREATE TABLE IF NOT EXISTS chase_items (
    practice_id       text        NOT NULL,
    chase_id          text        NOT NULL,
    patient_ref       text        NOT NULL,
    trigger           text        NOT NULL CHECK (trigger IN (
        'no_card_on_file', 'expired', 'eligibility_inactive',
        'eligibility_timeout', 'plan_year_rollover', 'manual',
        'denial_coverage'
    )),
    detected_at       text        NOT NULL,
    appointment_at    text        NOT NULL DEFAULT '',
    state             text        NOT NULL CHECK (state IN (
        'detected', 'needs_review', 'ready', 'in_progress', 'submitted',
        'verified_closed', 'escalated', 'unreachable', 'resolved_other'
    )),
    priority          integer     NOT NULL DEFAULT 0,
    bucket            text        NOT NULL CHECK (bucket IN (
        'missing', 'expired', 'eligibility'
    )),
    direct_contract   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    review_reason     text        NOT NULL DEFAULT '',
    assignee          text        NOT NULL DEFAULT '',
    closed_at         text        NOT NULL DEFAULT '',
    version           bigint      NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, chase_id),
    UNIQUE (practice_id, patient_ref, trigger)
);

CREATE INDEX IF NOT EXISTS chase_items_open_worklist_idx
    ON chase_items (practice_id, state, priority DESC, detected_at);
CREATE INDEX IF NOT EXISTS chase_items_assignee_idx
    ON chase_items (practice_id, assignee, state);

CREATE TABLE IF NOT EXISTS chase_attempts (
    practice_id       text             NOT NULL,
    chase_id          text             NOT NULL,
    attempt_no        integer          NOT NULL CHECK (attempt_no > 0),
    channel           text             NOT NULL CHECK (channel IN (
        'email', 'sms', 'portal', 'phone'
    )),
    template_id       text             NOT NULL,
    attempted_at      text             NOT NULL,
    actor             text             NOT NULL,
    outcome           text             NOT NULL,
    minutes_spent     double precision NOT NULL DEFAULT 0
                                      CHECK (minutes_spent >= 0),
    note              text             NOT NULL DEFAULT '',
    created_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, chase_id, attempt_no),
    FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chase_pending_tasks (
    practice_id       text        NOT NULL,
    chase_id          text        NOT NULL,
    payload           jsonb       NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, chase_id),
    FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chase_resolutions (
    practice_id       text        NOT NULL,
    chase_id          text        NOT NULL,
    payload           jsonb       NOT NULL,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (practice_id, chase_id),
    FOREIGN KEY (practice_id, chase_id)
        REFERENCES chase_items (practice_id, chase_id) ON DELETE CASCADE
);

COMMIT;
