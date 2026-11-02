-- dma-rims — the nightly export run
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- Schedule this file as a SQL task in a Databricks job, on the Analyst
-- Serverless warehouse, with one job parameter:
--
--     run_key = {{job.run_id}}
--
-- Then point the downstream export at dma_vw_gtin_country_blocked.
--
--
-- WHY THREE STATEMENTS, AND WHY THAT IS STILL SAFE
-- ------------------------------------------------
-- On Postgres this was one function in one transaction. Databricks SQL has
-- neither, so a run is: open the run row, record changes, close the run row.
-- Each statement is atomic on its own; the gaps between them are not.
--
--   * If the job dies after statement 1, the run stays 'running' forever.
--     dma_vw_export_run_log shows it, and that is the intended signal: a run
--     that never finished is visible, not silent.
--
--   * If the job dies after statement 2, the changes are recorded but the run
--     row has no counts. Re-running is safe: statement 2 compares against the
--     latest recorded answer, which now includes those rows, so nothing is
--     recorded twice.
--
--   * The counts in statement 3 are taken from a second read of the view, a
--     few seconds after statement 2's. A hold applied in that window would be
--     counted but not recorded until the next run. Acceptable for sanity
--     figures; do not treat them as the record.


-- 1. Open the run.
INSERT INTO cdw_prod.glb_bi.dma_export_run
    (run_key, started_at, run_by, status)
VALUES
    (:run_key, current_timestamp(), 'nightly-job', 'running');


-- 2. Record what changed since the last recorded answer.
INSERT INTO cdw_prod.glb_bi.dma_export_eligibility_history (
    run_key, recorded_at, gtin, sku, country_code, is_shippable,
    block_reason, detail, authorization_expiry, registration_expiry
)
WITH current_state AS (
    SELECT gtin,
           sku,
           country_code,
           is_shippable,
           block_reason,
           COALESCE(hold_reason, reason_comments) AS detail,
           authorization_expiry,
           registration_expiry
    FROM cdw_prod.glb_bi.dma_vw_gtin_country_eligibility
),
latest AS (
    SELECT h.gtin, h.country_code, h.is_shippable, h.block_reason
    FROM cdw_prod.glb_bi.dma_export_eligibility_history h
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY h.gtin, h.country_code
        ORDER BY h.recorded_at DESC, h.id DESC
    ) = 1
)
SELECT :run_key, current_timestamp(), c.gtin, c.sku, c.country_code,
       c.is_shippable, c.block_reason, c.detail,
       c.authorization_expiry, c.registration_expiry
FROM current_state c
LEFT JOIN latest l
       ON l.gtin = c.gtin
      AND l.country_code = c.country_code
-- IS DISTINCT FROM, not <>: block_reason is NULL when shippable, and
-- NULL <> 'ON HOLD' is NULL, which would silently drop the change.
WHERE l.gtin IS NULL
   OR l.is_shippable IS DISTINCT FROM c.is_shippable
   OR l.block_reason IS DISTINCT FROM c.block_reason;


-- 3. Close the run with its sanity figures.
MERGE INTO cdw_prod.glb_bi.dma_export_run AS r
USING (
    SELECT
        :run_key                                         AS run_key,
        count(*)                                         AS total_rows,
        count_if(is_shippable)                           AS shippable_rows,
        count_if(NOT is_shippable)                       AS blocked_rows,
        (SELECT count(*)
           FROM cdw_prod.glb_bi.dma_export_eligibility_history
          WHERE run_key = :run_key)                      AS changed_rows
    FROM cdw_prod.glb_bi.dma_vw_gtin_country_eligibility
) AS s
ON r.run_key = s.run_key
WHEN MATCHED THEN UPDATE SET
    r.finished_at    = current_timestamp(),
    r.status         = 'complete',
    r.total_rows     = s.total_rows,
    r.shippable_rows = s.shippable_rows,
    r.blocked_rows   = s.blocked_rows,
    r.changed_rows   = s.changed_rows;
