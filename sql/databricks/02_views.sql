-- dma-rims — derived status and the integration views, in cdw_prod.glb_bi
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- Run after 01_tables.sql. Replaces sql/views_integration.sql (Postgres) and
-- the status function and views from sql/schema.sql.
--
-- These views are the contract between this system and whatever reads it
-- downstream (Salespad / Korber / GP). Treat the column list as an interface:
-- adding columns is safe, renaming or removing them breaks the job.
--
-- One interface change from the Postgres version: country_alpha2 is now
-- country_iso_alpha3, because the country reference is cm_country_codes and
-- that is what it carries. country_code is the GP code.
--
--
-- THREE THINGS TO KNOW BEFORE BUILDING A JOB ON THIS
-- --------------------------------------------------
--
-- 1. The answer is computed at read time from current_date. Two reads on
--    different days can differ with no data having changed, because a licence
--    expired in between. Stamp the run time onto whatever is exported.
--
-- 2. A nightly cadence means a hold applied at 09:00 does not reach the
--    downstream systems until that night. If same-day holds must stop
--    shipment, a nightly read is not enough.
--
-- 3. These views say what is NOT allowed. They say nothing about whether stock
--    exists, which lot, or where. Joining to inventory is the consumer's job.


-- ---------------------------------------------------------------------------
-- Registration status: derived, never stored
-- ---------------------------------------------------------------------------
--
-- NOT DETERMINISTIC: the result depends on current_date. Declaring it
-- deterministic would let the engine cache a result, and an expired licence
-- would keep reading as active — the one failure this system exists to stop.

CREATE OR REPLACE FUNCTION cdw_prod.glb_bi.dma_registration_status(
    p_archived      BOOLEAN,
    p_pending_type  STRING,
    p_in_progress   BOOLEAN,
    p_expiry_date   DATE,
    p_expiring_days INT DEFAULT 90
)
RETURNS STRING
NOT DETERMINISTIC
COMMENT 'Single definition of registration status. Derived from expiry against today on every read.'
RETURN CASE
    WHEN p_archived                                              THEN 'Archived'
    WHEN p_pending_type IS NOT NULL                              THEN 'Pending'
    WHEN p_in_progress                                           THEN 'In progress'
    WHEN p_expiry_date IS NULL                                   THEN 'Active'
    WHEN p_expiry_date <  current_date()                         THEN 'Expired'
    WHEN p_expiry_date <= date_add(current_date(), p_expiring_days) THEN 'Expiring soon'
    ELSE 'Active'
END;


CREATE OR REPLACE VIEW cdw_prod.glb_bi.dma_registration_current AS
SELECT
    r.*,
    c.name AS country_name,
    cdw_prod.glb_bi.dma_registration_status(
        r.archived, r.pending_type, r.in_progress, r.expiry_date
    ) AS status,
    -- the alert window opens alert_months before expiry
    CASE WHEN r.expiry_date IS NULL THEN NULL
         ELSE date_sub(r.expiry_date, CAST(r.alert_months * 30.44 AS INT))
    END AS alert_opens_on,
    CASE WHEN r.expiry_date IS NULL
              OR r.pending_type IS NOT NULL
              OR r.in_progress THEN false
         ELSE current_date() >= date_sub(r.expiry_date, CAST(r.alert_months * 30.44 AS INT))
              AND r.expiry_date >= current_date()
    END AS in_alert_window
FROM cdw_prod.glb_bi.dma_registration r
LEFT JOIN cdw_prod.glb_bi.dma_country c
  ON c.code = r.country_code;


-- ---------------------------------------------------------------------------
-- Full picture: one row per GTIN x country
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW cdw_prod.glb_bi.dma_vw_gtin_country_eligibility AS
SELECT
    -- ---- the fields the downstream systems key on ----
    g.gtin,
    g.sku,
    g.country_code,                                             -- GP code
    c.iso_alpha3                        AS country_iso_alpha3,
    c.name                              AS country_name,
    g.status_for_sale,

    -- ---- the decision ----
    (
        g.status_for_sale = 'Approved'
        AND g.shippable
        AND (g.expiry_date IS NULL OR g.expiry_date >= current_date())
        AND (r.id IS NULL OR r.archived = false)
        AND (r.id IS NULL OR r.expiry_date IS NULL OR r.expiry_date >= current_date())
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
        WHEN g.expiry_date IS NOT NULL AND g.expiry_date < current_date()
            THEN 'AUTHORIZATION EXPIRED'
        WHEN r.id IS NOT NULL AND r.archived
            THEN 'REGISTRATION ARCHIVED'
        WHEN r.id IS NOT NULL AND r.expiry_date IS NOT NULL
             AND r.expiry_date < current_date()
            THEN 'REGISTRATION EXPIRED'
        ELSE NULL
    END                                 AS block_reason,

    -- ---- supporting detail, so the reason can be explained ----
    g.reason_comments,
    g.license_number,
    g.expiry_date                       AS authorization_expiry,
    r.registration_number,
    r.expiry_date                       AS registration_expiry,
    cdw_prod.glb_bi.dma_registration_status(
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
    GREATEST(
        g.updated_at,
        COALESCE(r.updated_at,         g.updated_at),
        COALESCE(hold.hold_applied_at, g.updated_at)
    )                                   AS last_changed_at,
    current_timestamp()                 AS generated_at

FROM cdw_prod.glb_bi.dma_gtin_authorization g
-- LEFT, not inner. Foreign keys are not enforced on Delta, so a row can carry
-- a country code that is not in cm_country_codes. An inner join would drop it
-- from this view, and so from the block list — which means it would never be
-- blocked. A row with an unknown country still appears, with a NULL name.
LEFT JOIN cdw_prod.glb_bi.dma_country c
  ON c.code = g.country_code
LEFT JOIN cdw_prod.glb_bi.dma_registration r
  ON r.id = g.registration_id
-- LATERAL with LIMIT 1, not a join to a set of holds. A GTIN can carry a
-- global hold (scope NULL) and a country hold at the same time; joining to
-- both would emit the row twice and duplicate it in the nightly export.
LEFT JOIN LATERAL (
    SELECT h.id         AS hold_id,
           h.reason     AS hold_reason,
           h.source_ref AS hold_source_ref,
           h.applied_at AS hold_applied_at
    FROM cdw_prod.glb_bi.dma_gtin_hold h
    WHERE h.gtin = g.gtin
      AND h.lifted_at IS NULL
      AND (h.scope IS NULL OR h.scope = g.country_code)
    ORDER BY h.applied_at DESC, h.id DESC
    LIMIT 1
) hold ON true;

COMMENT ON VIEW cdw_prod.glb_bi.dma_vw_gtin_country_eligibility IS
  'Integration contract: may this GTIN ship into this country, and if not why. Computed at read time from current_date; stamp the run time onto whatever is exported. Adding columns is safe; renaming or removing them is not.';


-- ---------------------------------------------------------------------------
-- The minimal feed: what must NOT ship
-- ---------------------------------------------------------------------------
--
-- A block list rather than an allow list, deliberately: if the export fails or
-- arrives empty, nothing is wrongly released. Decide explicitly how the
-- consumer behaves on a stale or missing file.

CREATE OR REPLACE VIEW cdw_prod.glb_bi.dma_vw_gtin_country_blocked AS
SELECT
    gtin,
    sku,
    country_code,
    country_iso_alpha3,
    country_name,
    block_reason,
    COALESCE(hold_reason, reason_comments) AS detail,
    hold_source_ref,
    authorization_expiry,
    registration_expiry,
    last_changed_at,
    generated_at
FROM cdw_prod.glb_bi.dma_vw_gtin_country_eligibility
WHERE is_shippable = false;

COMMENT ON VIEW cdw_prod.glb_bi.dma_vw_gtin_country_blocked IS
  'Rows that must not ship. The nightly export payload. Exporting blocks rather than permissions means a failed or empty run suppresses nothing rather than releasing everything.';


-- ---------------------------------------------------------------------------
-- Expiring soon: what is about to become a block
-- ---------------------------------------------------------------------------
--
-- Not part of the shipping control. Lets the nightly job raise the warning
-- that gets a renewal started before product stops moving.

CREATE OR REPLACE VIEW cdw_prod.glb_bi.dma_vw_gtin_country_expiring AS
WITH e AS (
    SELECT
        *,
        LEAST(
            COALESCE(authorization_expiry, DATE '9999-12-31'),
            COALESCE(registration_expiry,  DATE '9999-12-31')
        ) AS expires_on
    FROM cdw_prod.glb_bi.dma_vw_gtin_country_eligibility
    WHERE is_shippable
)
SELECT
    gtin,
    sku,
    country_code,
    country_iso_alpha3,
    country_name,
    expires_on,
    datediff(expires_on, current_date()) AS days_remaining,
    registration_number,
    registration_status,
    generated_at
FROM e
WHERE expires_on <= date_add(current_date(), 90);
