-- dma-rims — integration views for the nightly export
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- These views are the contract between this system and whatever reads it
-- downstream (Salespad / Korber / GP). Treat the column list as an interface:
-- adding columns is safe, renaming or removing them breaks the job.
--
-- Run after schema.sql.
--
--
-- WHAT THESE ANSWER
-- -----------------
-- For a given GTIN in a given country: may it ship, and if not, why not.
-- That is the decision the manual Power BI / spreadsheet / RIMS cross-
-- reference currently produces by hand.
--
--
-- THREE THINGS TO KNOW BEFORE BUILDING A JOB ON THIS
-- --------------------------------------------------
--
-- 1. The answer is computed at read time from current_date. Two reads on
--    different days can differ with no data having changed, because a licence
--    expired in between. That is intended — but it means the job's run time is
--    part of its result, and the result should be stamped with it.
--
-- 2. A nightly cadence means a hold applied at 09:00 does not reach the
--    downstream systems until that night. For the window in between, the only
--    control is the manual one. If same-day holds need to stop shipment, this
--    needs a trigger on hold insert, not a nightly read.
--
-- 3. These views tell you what is NOT allowed. They say nothing about whether
--    stock exists, which lot it is, or where it sits. Joining to inventory is
--    the consuming system's job.

BEGIN;

SET search_path TO dma, public;


-- ---------------------------------------------------------------------------
-- Full picture: one row per GTIN x country
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW vw_gtin_country_eligibility AS
SELECT
    -- ---- the four fields the downstream systems key on ----
    g.gtin,
    g.sku,
    c.code                              AS country_code,     -- ISO 3166-1 numeric
    c.alpha2                            AS country_alpha2,
    c.name                              AS country_name,
    g.status_for_sale,

    -- ---- the decision ----
    (
        g.status_for_sale = 'Approved'
        AND g.shippable
        AND (g.expiry_date IS NULL OR g.expiry_date >= current_date)
        AND (r.id IS NULL OR r.archived = false)
        AND (r.id IS NULL OR r.expiry_date IS NULL OR r.expiry_date >= current_date)
        AND hold.hold_id IS NULL
    )                                   AS is_shippable,

    -- First failing condition, in the order a human would check them. NULL
    -- when the GTIN may ship. Customer Service needs the reason, not just the
    -- refusal.
    CASE
        WHEN hold.hold_id IS NOT NULL
            THEN 'ON HOLD'
        WHEN g.status_for_sale <> 'Approved'
            THEN 'NOT APPROVED FOR SALE'
        WHEN NOT g.shippable
            THEN 'BLOCKED'
        WHEN g.expiry_date IS NOT NULL AND g.expiry_date < current_date
            THEN 'AUTHORIZATION EXPIRED'
        WHEN r.id IS NOT NULL AND r.archived
            THEN 'REGISTRATION ARCHIVED'
        WHEN r.id IS NOT NULL AND r.expiry_date IS NOT NULL
             AND r.expiry_date < current_date
            THEN 'REGISTRATION EXPIRED'
        ELSE NULL
    END                                 AS block_reason,

    -- ---- supporting detail, so the reason can be explained ----
    g.reason_comments,
    g.license_number,
    g.expiry_date                       AS authorization_expiry,
    r.registration_number,
    r.expiry_date                       AS registration_expiry,
    registration_status(
        COALESCE(r.archived, false), r.pending_type,
        COALESCE(r.in_progress, false), r.expiry_date
    )                                   AS registration_status,
    hold.hold_id,
    hold.hold_reason,
    hold.hold_source_ref,
    hold.hold_applied_at,

    g.is_kit,
    g.item_description,
    g.vendor,
    g.source_system,

    -- ---- freshness ----
    -- Lets a job take deltas instead of a full reload, and lets anyone reading
    -- the export see how old the underlying record is.
    GREATEST(
        g.updated_at,
        COALESCE(r.updated_at,        g.updated_at),
        COALESCE(hold.hold_applied_at, g.updated_at)
    )                                   AS last_changed_at,
    now()                               AS generated_at

FROM gtin_authorization g
JOIN country c
  ON c.code = g.country_code
LEFT JOIN registration r
  ON r.id = g.registration_id
-- LATERAL with LIMIT 1, not a join to a set of holds. A GTIN can carry a
-- global hold (scope NULL) and a country hold at the same time; joining to
-- both would emit the row twice and duplicate it in the nightly export. The
-- most recent hold wins, and it only has to exist for the row to be blocked.
LEFT JOIN LATERAL (
    SELECT h.id         AS hold_id,
           h.reason     AS hold_reason,
           h.source_ref AS hold_source_ref,
           h.applied_at AS hold_applied_at
    FROM gtin_hold h
    WHERE h.gtin = g.gtin
      AND h.lifted_at IS NULL
      AND (h.scope IS NULL OR h.scope = g.country_code)
    ORDER BY h.applied_at DESC, h.id DESC
    LIMIT 1
) hold ON true;

COMMENT ON VIEW vw_gtin_country_eligibility IS
  'Integration contract: may this GTIN ship into this country, and if not why. '
  'Computed at read time from current_date — stamp the run time onto whatever '
  'is exported. Adding columns is safe; renaming or removing them is not.';


-- ---------------------------------------------------------------------------
-- The minimal feed: what must NOT ship
-- ---------------------------------------------------------------------------
--
-- This is the payload for inventory suppression and bin pre-filtering. A
-- block list is deliberately the thing that gets exported rather than an allow
-- list: if the export fails or arrives empty, nothing is wrongly suppressed,
-- and the failure shows up as "no new blocks" rather than as "everything is
-- shippable". Decide explicitly how the consumer should behave on a stale or
-- missing file — an allow list that fails open is how product ships to a
-- country it is not registered in.

CREATE OR REPLACE VIEW vw_gtin_country_blocked AS
SELECT
    gtin,
    sku,
    country_code,
    country_alpha2,
    country_name,
    block_reason,
    COALESCE(hold_reason, reason_comments)  AS detail,
    hold_source_ref,
    authorization_expiry,
    registration_expiry,
    last_changed_at,
    generated_at
FROM vw_gtin_country_eligibility
WHERE is_shippable = false;

COMMENT ON VIEW vw_gtin_country_blocked IS
  'Rows that must not ship. The nightly export payload. Exporting blocks '
  'rather than permissions means a failed or empty run suppresses nothing '
  'rather than releasing everything.';


-- ---------------------------------------------------------------------------
-- Expiring soon: what is about to become a block
-- ---------------------------------------------------------------------------
--
-- Not part of the shipping control. This is so the nightly job can also raise
-- the warning that lets someone renew before product stops moving.

CREATE OR REPLACE VIEW vw_gtin_country_expiring AS
SELECT
    gtin,
    sku,
    country_code,
    country_alpha2,
    country_name,
    LEAST(
        COALESCE(authorization_expiry, DATE '9999-12-31'),
        COALESCE(registration_expiry,  DATE '9999-12-31')
    )                                                   AS expires_on,
    LEAST(
        COALESCE(authorization_expiry, DATE '9999-12-31'),
        COALESCE(registration_expiry,  DATE '9999-12-31')
    ) - current_date                                    AS days_remaining,
    registration_number,
    registration_status,
    generated_at
FROM vw_gtin_country_eligibility
WHERE is_shippable
  AND LEAST(
        COALESCE(authorization_expiry, DATE '9999-12-31'),
        COALESCE(registration_expiry,  DATE '9999-12-31')
      ) <= current_date + INTERVAL '90 days';


-- ---------------------------------------------------------------------------
-- Access for the job
-- ---------------------------------------------------------------------------
--
-- The nightly job should read through its own principal with SELECT on these
-- views and nothing else, so an export job can never write. Replace the
-- placeholder and run separately once the principal exists.
--
--   GRANT USAGE ON SCHEMA dma TO "REPLACE_JOB_PRINCIPAL";
--   GRANT SELECT ON dma.vw_gtin_country_eligibility,
--                   dma.vw_gtin_country_blocked,
--                   dma.vw_gtin_country_expiring
--       TO "REPLACE_JOB_PRINCIPAL";

COMMIT;
