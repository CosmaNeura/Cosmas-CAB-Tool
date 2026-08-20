-- Admit only the explicit, fixed-purpose synthetic demo seed provenance.
-- Existing source values remain unchanged; arbitrary source labels stay closed.

BEGIN;

ALTER TABLE encounters
    DROP CONSTRAINT encounters_source_valid;

ALTER TABLE encounters
    ADD CONSTRAINT encounters_source_valid CHECK (
        source IN (
            'synthetic',
            'deidentified',
            'manual',
            'athena',
            'explicit_import',
            'synthetic_demo_release_candidate'
        )
    );

COMMIT;
