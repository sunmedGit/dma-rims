-- dma-rims — export run history, in cdw_prod.glb_bi
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- Run after 02_views.sql. Replaces sql/export_snapshot.sql (Postgres).
--
-- WHAT THIS IS FOR
-- ----------------
-- One question, asked after the fact: "what did this system say about GTIN X
-- in country Y on 3 March?" The views cannot answer it — they are computed
-- from current_date and have no memory.
--
-- A history row is written only when a GTIN x country answer differs from the
-- last recorded one. Every run is recorded in dma_export_run whether or not
-- anything changed, so "nothing changed" and "the job did not run" stay
-- distinguishable.
--
-- The run itself is in 04_nightly_job.sql. On Postgres it was one function in
-- one transaction; Databricks SQL has no procedural functions and no
-- multi-statement transactions, so it is three statements instead. That file
-- explains what that costs and why it is still safe to re-run.


-- ---------------------------------------------------------------------------
-- One row per run of the nightly job
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_export_run (
    run_key        STRING    NOT NULL COMMENT 'Supplied by the job, e.g. the Databricks job run id. Not an identity column: Databricks SQL has no INSERT ... RETURNING, so the job needs to know its own key before it writes.',
    started_at     TIMESTAMP NOT NULL,
    finished_at    TIMESTAMP,
    run_by         STRING    NOT NULL,
    status         STRING    NOT NULL,

    -- Sanity figures. A run whose total collapses from 200,000 to 12 has not
    -- found that everything became eligible; it has failed in a way that would
    -- otherwise look like success.
    total_rows     BIGINT,
    shippable_rows BIGINT,
    blocked_rows   BIGINT,
    changed_rows   BIGINT,

    note           STRING
) USING DELTA
COMMENT 'One row per run, recorded even when nothing changed, so a job that did not run is distinguishable from a night with no changes. A run left in status running did not finish.';

ALTER TABLE cdw_prod.glb_bi.dma_export_run
    ADD CONSTRAINT dma_export_run_status_ck
    CHECK (status IN ('running', 'complete', 'failed'));

ALTER TABLE cdw_prod.glb_bi.dma_export_run
    ADD CONSTRAINT dma_export_run_pk PRIMARY KEY (run_key);


-- ---------------------------------------------------------------------------
-- What changed, and when
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_export_eligibility_history (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY,
    run_key              STRING    NOT NULL,
    recorded_at          TIMESTAMP NOT NULL,

    gtin                 STRING    NOT NULL,
    sku                  STRING,
    country_code         STRING    NOT NULL,

    is_shippable         BOOLEAN   NOT NULL,
    block_reason         STRING,
    detail               STRING,
    authorization_expiry DATE,
    registration_expiry  DATE
) USING DELTA
CLUSTER BY (gtin, country_code)
COMMENT 'Append-only by job discipline: the job inserts, nothing updates or deletes. Unity Catalog cannot enforce that by grant (MODIFY covers all three), so DESCRIBE HISTORY is the check. A row is written only when an answer changes, so the absence of a row means no change.';

ALTER TABLE cdw_prod.glb_bi.dma_export_eligibility_history
    ADD CONSTRAINT dma_export_hist_run_fk
    FOREIGN KEY (run_key) REFERENCES cdw_prod.glb_bi.dma_export_run(run_key)
    NOT ENFORCED;


-- ---------------------------------------------------------------------------
-- Reading history back
-- ---------------------------------------------------------------------------
--
--     SELECT * FROM cdw_prod.glb_bi.dma_eligibility_as_of(TIMESTAMP '2026-03-03 23:59:59')
--     WHERE gtin = '00812457000934';

CREATE OR REPLACE FUNCTION cdw_prod.glb_bi.dma_eligibility_as_of(p_when TIMESTAMP)
RETURNS TABLE (
    gtin         STRING,
    sku          STRING,
    country_code STRING,
    is_shippable BOOLEAN,
    block_reason STRING,
    detail       STRING,
    recorded_at  TIMESTAMP,
    run_key      STRING
)
COMMENT 'What the system said at a given moment: the latest recorded answer per GTIN x country at or before p_when.'
RETURN
    SELECT h.gtin, h.sku, h.country_code, h.is_shippable, h.block_reason,
           h.detail, h.recorded_at, h.run_key
    FROM cdw_prod.glb_bi.dma_export_eligibility_history h
    WHERE h.recorded_at <= p_when
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY h.gtin, h.country_code
        ORDER BY h.recorded_at DESC, h.id DESC
    ) = 1;


-- Runs with the change in row count against the previous run. The first place
-- to look when someone asks whether last night's export actually happened.
CREATE OR REPLACE VIEW cdw_prod.glb_bi.dma_vw_export_run_log AS
SELECT
    r.run_key,
    r.started_at,
    r.finished_at,
    timestampdiff(SECOND, r.started_at, r.finished_at)   AS duration_seconds,
    r.status,
    r.run_by,
    r.total_rows,
    r.shippable_rows,
    r.blocked_rows,
    r.changed_rows,
    LAG(r.total_rows) OVER (ORDER BY r.started_at)       AS previous_total_rows,
    r.total_rows - LAG(r.total_rows) OVER (ORDER BY r.started_at)
                                                         AS total_rows_delta,
    r.note
FROM cdw_prod.glb_bi.dma_export_run r;

COMMENT ON VIEW cdw_prod.glb_bi.dma_vw_export_run_log IS
  'Run history with the change in row count against the previous run. A large negative delta is a failed run that looks like success.';
