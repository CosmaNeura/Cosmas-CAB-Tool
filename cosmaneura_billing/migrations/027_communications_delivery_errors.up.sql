-- Expand only: retain the validated code without rewriting callback evidence.
BEGIN;
ALTER TABLE chase_communication_provider_events
    ADD COLUMN error_code text CHECK (error_code IS NULL OR
        (error_code ~ '^[0-9]{1,6}$' AND kind IN ('sms_status','voice_status','voice_gather')));
COMMIT;
