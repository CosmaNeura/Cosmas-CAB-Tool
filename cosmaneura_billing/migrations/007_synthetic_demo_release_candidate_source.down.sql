-- Restore the pre-release-candidate source allowlist.  This intentionally
-- fails closed while any release-candidate seed row still exists.

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
            'explicit_import'
        )
    );

COMMIT;
