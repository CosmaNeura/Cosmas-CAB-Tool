-- Expand-only rollback: disable the separately composed Core bridge.
-- Preserve identity provenance, mappings and all source/header history.
BEGIN;
SELECT 1;
COMMIT;
