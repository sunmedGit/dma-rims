-- dma-rims — export run history
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- Run after schema.sql and views_integration.sql.
--
--
-- WHAT THIS IS FOR
-- ----------------
-- One question, asked after the fact: "what did this system say about GTIN X
-- in country Y on 3 March?" If the nightly export ends up suppressing
-- inventory or pre-filtering bins, that question will be asked — by an
-- auditor, or by someone investigating a shipment that should not have gone.
-- The views alone cannot answer it, because they are computed from
-- current_date and have no memory.
--
--
-- WHY CHANGES AND NOT NIGHTLY FULL COPIES
-- ---------------------------------------
-- A row is written only when a GTIN x country's answer differs from the last
-- recorded one. Storing every row every night would be a few hundred thousand
-- rows a day to record a handful of real changes, and the volume would make
-- the history the first thing someone proposes to delete.
--
-- Every run is recorded in export_run whether or not anything changed, so
-- "nothing changed" and "the job did not run" stay distinguishable. That
-- distinction is the point: silence from a control is not the same as the
-- control saying yes.

BEGIN;

SET search_path TO dma, public;


-- ---------------------------------------------------------------------------
-- One row per run of the nightly job
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS export_run (
    id              bigserial PRIMARY KEY,
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    run_by          text NOT NULL,
    status          text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'failed')),

    -- Sanity figures for the run. A run whose total collapses from 200,000 to
    -- 12 has not found that everything became eligible; it has failed in a way
    -- that would otherwise look like success.
    total_rows      integer,
    shippable_rows  integer,
    blocked_rows    integer,
    changed_rows    integer,

    note            text
);

CREATE INDEX IF NOT EXISTS export_run_started_idx ON export_run (started_at DESC);

COMMENT ON TABLE export_run IS
  'One row per run, recorded even when nothing changed, so a job that did not '
  'run is distinguishable from a night with no changes.';


-- ---------------------------------------------------------------------------
-- What changed, and when
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS export_eligibility_history (
    id                   bigserial PRIMARY KEY,
    run_id               bigint NOT NULL REFERENCES export_run(id),
    recorded_at          timestamptz NOT NULL DEFAULT now(),

    gtin                 text NOT NULL,
    sku                  text,
    country_code         text NOT NULL,

    is_shippable         boolean NOT NULL,
    block_reason         text,
    detail               text,
    authorization_expiry date,
    registration_expiry  date
);

CREATE INDEX IF NOT EXISTS export_hist_key_idx
    ON export_eligibility_history (gtin, country_code, recorded_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS export_hist_run_idx
    ON export_eligibility_history (run_id);
CREATE INDEX IF NOT EXISTS export_hist_blocked_idx
    ON export_eligibility_history (recorded_at DESC)
    WHERE is_shippable = false;

COMMENT ON TABLE export_eligibility_history IS
  'Append-only. A row is written only when a GTIN x country answer differs '
  'from the last recorded one, so the absence of a row means no change.';


-- ---------------------------------------------------------------------------
-- Record a run
-- ---------------------------------------------------------------------------
--
-- The whole nightly job, as far as this database is concerned, is:
--
--     SELECT dma.record_export_run('nightly-job');
--
-- One statement, one transaction. Either the run row and its changes are both
-- recorded or neither is, so there is no state where changes exist with no run
-- that produced them.

CREATE OR REPLACE FUNCTION record_export_run(
    p_run_by text DEFAULT 'nightly-job',
    p_note   text DEFAULT NULL
) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    v_run_id   bigint;
    v_total    integer;
    v_ship     integer;
    v_blocked  integer;
    v_changed  integer;
BEGIN
    INSERT INTO export_run (run_by, note)
    VALUES (p_run_by, p_note)
    RETURNING id INTO v_run_id;

    WITH current_state AS (
        SELECT gtin,
               sku,
               country_code,
               is_shippable,
               block_reason,
               COALESCE(hold_reason, reason_comments) AS detail,
               authorization_expiry,
               registration_expiry
        FROM vw_gtin_country_eligibility
    ),
    latest AS (
        SELECT DISTINCT ON (h.gtin, h.country_code)
               h.gtin, h.country_code, h.is_shippable, h.block_reason
        FROM export_eligibility_history h
        ORDER BY h.gtin, h.country_code, h.recorded_at DESC, h.id DESC
    ),
    inserted AS (
        INSERT INTO export_eligibility_history (
            run_id, gtin, sku, country_code, is_shippable, block_reason,
            detail, authorization_expiry, registration_expiry
        )
        SELECT v_run_id, c.gtin, c.sku, c.country_code, c.is_shippable,
               c.block_reason, c.detail, c.authorization_expiry,
               c.registration_expiry
        FROM current_state c
        LEFT JOIN latest l
               ON l.gtin = c.gtin
              AND l.country_code = c.country_code
        -- IS DISTINCT FROM, not <>: block_reason is NULL when shippable, and
        -- NULL <> 'ON HOLD' is NULL, which would silently drop the change.
        WHERE l.gtin IS NULL
           OR l.is_shippable IS DISTINCT FROM c.is_shippable
           OR l.block_reason IS DISTINCT FROM c.block_reason
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM current_state),
           (SELECT count(*) FROM current_state WHERE is_shippable),
           (SELECT count(*) FROM current_state WHERE NOT is_shippable),
           (SELECT count(*) FROM inserted)
      INTO v_total, v_ship, v_blocked, v_changed;

    UPDATE export_run
       SET finished_at    = now(),
           status         = 'complete',
           total_rows     = v_total,
           shippable_rows = v_ship,
           blocked_rows   = v_blocked,
           changed_rows   = v_changed
     WHERE id = v_run_id;

    RETURN v_run_id;
END;
$$;

COMMENT ON FUNCTION record_export_run(text, text) IS
  'Records one export run and any changed eligibility rows, in one '
  'transaction. Returns the run id.';


-- ---------------------------------------------------------------------------
-- Reading history back
-- ---------------------------------------------------------------------------
--
-- What the system said at a given moment: the most recent recorded answer for
-- each GTIN x country at or before that time.
--
--     SELECT * FROM dma.eligibility_as_of('2026-03-03 23:59:59+00')
--     WHERE gtin = '00812457000934';

CREATE OR REPLACE FUNCTION eligibility_as_of(p_when timestamptz)
RETURNS TABLE (
    gtin         text,
    sku          text,
    country_code text,
    is_shippable boolean,
    block_reason text,
    detail       text,
    recorded_at  timestamptz,
    run_id       bigint
)
LANGUAGE sql STABLE AS $$
    SELECT DISTINCT ON (h.gtin, h.country_code)
           h.gtin, h.sku, h.country_code, h.is_shippable, h.block_reason,
           h.detail, h.recorded_at, h.run_id
    FROM export_eligibility_history h
    WHERE h.recorded_at <= p_when
    ORDER BY h.gtin, h.country_code, h.recorded_at DESC, h.id DESC;
$$;


-- Runs, newest first, with how long each took. The first place to look when
-- someone asks whether last night's export actually happened.
CREATE OR REPLACE VIEW vw_export_run_log AS
SELECT
    r.id,
    r.started_at,
    r.finished_at,
    r.finished_at - r.started_at AS duration,
    r.status,
    r.run_by,
    r.total_rows,
    r.shippable_rows,
    r.blocked_rows,
    r.changed_rows,
    LAG(r.total_rows) OVER (ORDER BY r.started_at)        AS previous_total_rows,
    r.total_rows - LAG(r.total_rows) OVER (ORDER BY r.started_at)
                                                          AS total_rows_delta,
    r.note
FROM export_run r
ORDER BY r.started_at DESC;

COMMENT ON VIEW vw_export_run_log IS
  'Run history with the change in row count against the previous run. A large '
  'negative delta is a failed run that looks like success.';


-- ---------------------------------------------------------------------------
-- Append-only
-- ---------------------------------------------------------------------------
--
-- History is evidence. The job inserts; nothing updates or deletes.

CREATE OR REPLACE RULE export_history_no_update AS
    ON UPDATE TO export_eligibility_history DO INSTEAD NOTHING;

CREATE OR REPLACE RULE export_history_no_delete AS
    ON DELETE TO export_eligibility_history DO INSTEAD NOTHING;


-- ---------------------------------------------------------------------------
-- Access for the job
-- ---------------------------------------------------------------------------
--
-- The job needs to call the function, which inserts on its behalf. Granting
-- EXECUTE rather than table INSERT keeps the shape of what it may write fixed.
--
--   GRANT USAGE ON SCHEMA dma TO "REPLACE_JOB_PRINCIPAL";
--   GRANT SELECT ON dma.vw_gtin_country_eligibility,
--                   dma.vw_gtin_country_blocked,
--                   dma.vw_gtin_country_expiring,
--                   dma.vw_export_run_log
--       TO "REPLACE_JOB_PRINCIPAL";
--   GRANT EXECUTE ON FUNCTION dma.record_export_run(text, text)
--       TO "REPLACE_JOB_PRINCIPAL";
--   GRANT EXECUTE ON FUNCTION dma.eligibility_as_of(timestamptz)
--       TO "REPLACE_JOB_PRINCIPAL";
--
-- Note: record_export_run runs with the caller's rights, so the principal also
-- needs INSERT on export_run and export_eligibility_history and UPDATE on
-- export_run. Make the function SECURITY DEFINER instead if you would rather
-- the job hold no table privileges at all — decide this with whoever reviews
-- the grants, since SECURITY DEFINER moves the trust into the function owner.

COMMIT;
