-- Destructive rollback for shared source-import state. Close every importer
-- first. Receipt/checkpoint history is dropped; the rollback intentionally
-- fails closed while an encounter with reserved Preview provenance remains.

BEGIN;

-- Snapshot both registered and orphaned importer-like roles while the source
-- tables still exist. An accidentally deleted binding must not make a role
-- disappear from rollback revocation discovery.
CREATE TEMP TABLE source_import_roles_to_revoke (
    role_name name PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO source_import_roles_to_revoke (role_name)
SELECT binding.role_name
  FROM public.source_import_role_bindings AS binding;

INSERT INTO source_import_roles_to_revoke (role_name)
SELECT role_row.rolname
  FROM pg_catalog.pg_class AS relation
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
 CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS privilege
  JOIN pg_catalog.pg_roles AS role_row
    ON role_row.oid = privilege.grantee
 WHERE namespace.nspname = 'public'
   AND relation.relname IN (
       'source_import_receipts', 'source_import_checkpoints'
   )
   AND privilege.privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
   AND role_row.rolname !~ '^pg_'
   AND role_row.rolname <> pg_catalog.pg_get_userbyid(relation.relowner)
ON CONFLICT (role_name) DO NOTHING;

-- Include residual column-level grants from an interrupted or older
-- provisioner. pg_class.relacl does not contain pg_attribute.attacl entries.
INSERT INTO source_import_roles_to_revoke (role_name)
SELECT role_row.rolname
  FROM pg_catalog.pg_attribute AS attribute
  JOIN pg_catalog.pg_class AS relation
    ON relation.oid = attribute.attrelid
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = relation.relnamespace
 CROSS JOIN LATERAL pg_catalog.aclexplode(attribute.attacl) AS privilege
  JOIN pg_catalog.pg_roles AS role_row
    ON role_row.oid = privilege.grantee
 WHERE namespace.nspname = 'public'
   AND relation.relname IN (
       'source_import_receipts', 'source_import_checkpoints'
   )
   AND privilege.privilege_type IN ('SELECT', 'INSERT', 'UPDATE')
   AND role_row.rolname !~ '^pg_'
   AND role_row.rolname <> pg_catalog.pg_get_userbyid(relation.relowner)
ON CONFLICT (role_name) DO NOTHING;

-- Include an orphan retaining only EXECUTE on the atomic page primitive.
INSERT INTO source_import_roles_to_revoke (role_name)
SELECT role_row.rolname
  FROM pg_catalog.pg_proc AS procedure
  JOIN pg_catalog.pg_namespace AS namespace
    ON namespace.oid = procedure.pronamespace
 CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) AS privilege
  JOIN pg_catalog.pg_roles AS role_row
    ON role_row.oid = privilege.grantee
 WHERE namespace.nspname = 'public'
   AND procedure.oid = pg_catalog.to_regprocedure(
       'public.cosmaneura_commit_source_encounter_page('
       'text,text,text,text,text,bigint,jsonb,text,text)'
   )
   AND privilege.privilege_type = 'EXECUTE'
   AND role_row.rolname !~ '^pg_'
   AND role_row.rolname <> pg_catalog.pg_get_userbyid(procedure.proowner)
ON CONFLICT (role_name) DO NOTHING;

-- Destroy state and remove each binding while its deletion guard can still
-- fail closed if a registered provenance has any encounter left.
DO $$
DECLARE
    binding record;
    importer record;
BEGIN
    FOR binding IN
        SELECT source_binding.role_name, source_binding.practice_id
          FROM public.source_import_role_bindings AS source_binding
    LOOP
        PERFORM set_config('cosmaneura.practice_id', binding.practice_id, true);
        DELETE FROM public.source_import_receipts
         WHERE practice_id = binding.practice_id;
        DELETE FROM public.source_import_checkpoints
         WHERE practice_id = binding.practice_id;
        DELETE FROM public.source_import_role_bindings
         WHERE role_name = binding.role_name;
    END LOOP;

    FOR importer IN SELECT role_name FROM source_import_roles_to_revoke
    LOOP
        IF EXISTS (
            SELECT 1
              FROM pg_catalog.pg_roles AS role_row
             WHERE role_row.rolname = importer.role_name
        ) THEN
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON TABLE public.encounters, '
                'public.source_import_checkpoints, '
                'public.source_import_receipts FROM %I',
                importer.role_name
            );
            EXECUTE format(
                'REVOKE EXECUTE ON FUNCTION '
                'public.cosmaneura_practice_allowed(text), '
                'public.cosmaneura_source_import_owner(), '
                'public.cosmaneura_source_import_session(), '
                'public.cosmaneura_source_import_allowed(text, text, text) '
                'FROM %I',
                importer.role_name
            );
            IF pg_catalog.to_regprocedure(
                'public.cosmaneura_commit_source_encounter_page('
                'text,text,text,text,text,bigint,jsonb,text,text)'
            ) IS NOT NULL THEN
                EXECUTE format(
                    'REVOKE EXECUTE ON FUNCTION '
                    'public.cosmaneura_commit_source_encounter_page('
                    'text, text, text, text, text, bigint, jsonb, text, text) '
                    'FROM %I',
                    importer.role_name
                );
            END IF;
            EXECUTE format('ALTER ROLE %I NOLOGIN', importer.role_name);
        END IF;
        DELETE FROM public.runtime_practice_bindings
         WHERE role_name = importer.role_name;
    END LOOP;
END
$$;

DROP FUNCTION IF EXISTS public.cosmaneura_commit_source_encounter_page(
    text, text, text, text, text, bigint, jsonb, text, text
);

DROP TRIGGER IF EXISTS source_import_receipts_truncate_guard
    ON public.source_import_receipts;
DROP TRIGGER IF EXISTS source_import_receipts_guard
    ON public.source_import_receipts;
DROP FUNCTION IF EXISTS public.cosmaneura_guard_source_import_receipt();

DROP TRIGGER IF EXISTS source_import_checkpoints_truncate_guard
    ON public.source_import_checkpoints;
DROP TRIGGER IF EXISTS source_import_checkpoints_guard
    ON public.source_import_checkpoints;
DROP FUNCTION IF EXISTS public.cosmaneura_guard_source_import_checkpoint();

DROP TRIGGER IF EXISTS encounters_z_source_import_truncate_guard
    ON public.encounters;
DROP TRIGGER IF EXISTS encounters_source_import_truncate_guard
    ON public.encounters;
DROP TRIGGER IF EXISTS encounters_source_import_guard
    ON public.encounters;
DROP FUNCTION IF EXISTS public.cosmaneura_guard_source_import_encounter();

DROP POLICY IF EXISTS encounters_source_import_restriction
    ON public.encounters;
DROP POLICY IF EXISTS source_import_receipts_isolation
    ON public.source_import_receipts;
DROP POLICY IF EXISTS source_import_checkpoints_isolation
    ON public.source_import_checkpoints;

DROP FUNCTION IF EXISTS public.cosmaneura_source_import_allowed(
    text, text, text
);
DROP FUNCTION IF EXISTS public.cosmaneura_source_import_owner();
DROP FUNCTION IF EXISTS public.cosmaneura_source_import_session();

DROP TABLE IF EXISTS public.source_import_receipts;
DROP TABLE IF EXISTS public.source_import_checkpoints;

DROP TRIGGER IF EXISTS source_import_role_bindings_truncate_guard
    ON public.source_import_role_bindings;
DROP TRIGGER IF EXISTS source_import_role_bindings_guard
    ON public.source_import_role_bindings;
DROP FUNCTION IF EXISTS public.cosmaneura_guard_source_import_binding();

DROP TABLE IF EXISTS public.source_import_role_bindings;

ALTER TABLE public.runtime_practice_bindings
    DROP CONSTRAINT runtime_practice_bindings_role_practice_unique;

ALTER TABLE public.encounters
    DROP CONSTRAINT encounters_uid_source_unique;

ALTER TABLE public.encounters
    DROP CONSTRAINT encounters_source_valid;

ALTER TABLE public.encounters
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
