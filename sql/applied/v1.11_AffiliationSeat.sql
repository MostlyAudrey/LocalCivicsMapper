-- v2.1  Seats on a body ("District 3", "Trustee Area 4") are a field of the affiliation, not a separate entity.
--
-- seat                 normalized seat label, NULL when unknown or the body has no numbered seats
-- seat_verified_at     set when an admin confirmed the seat against an outside source; a verified seat is never
--                      overwritten by article extraction
-- seat_verified_source where it was checked (a URL or a short note)
BEGIN;

ALTER TABLE TB_Entity_Affiliation ADD COLUMN IF NOT EXISTS seat TEXT;
ALTER TABLE TB_Entity_Affiliation ADD COLUMN IF NOT EXISTS seat_verified_at TIMESTAMPTZ;
ALTER TABLE TB_Entity_Affiliation ADD COLUMN IF NOT EXISTS seat_verified_source TEXT;

CREATE INDEX IF NOT EXISTS ix_affiliation_org_seat
    ON TB_Entity_Affiliation (organization_id, seat) WHERE seat IS NOT NULL;

COMMIT;
