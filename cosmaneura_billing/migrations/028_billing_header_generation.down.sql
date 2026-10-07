-- Recovery disables async generation; preserve claims, audit and header history.
BEGIN;
SELECT 1;
COMMIT;
