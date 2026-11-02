-- dma-rims — database roles and grants
--
-- DRAFT — requires QA/RA review.
--
-- Run separately from schema.sql, after reviewing. Replace :app_principal with
-- the Databricks App's service principal as it appears in Lakebase.
--
-- How the two access levels actually work
-- ---------------------------------------
-- The app connects to Lakebase as ONE service principal, so Postgres cannot
-- tell an Editor from a Viewer. The Editor/Viewer split is enforced in the API
-- layer from the caller's Databricks workspace group membership.
--
-- That is worth stating plainly: the database does not enforce it, the
-- application does. Anyone who can reach the database directly bypasses it.
-- Direct database access should therefore be restricted to administrators.
--
-- The one guarantee that IS enforced here is the audit trail: the application
-- principal gets INSERT and SELECT on audit_log and nothing else, so the app
-- cannot rewrite history even if its own code is wrong.

\set app_principal 'REPLACE_ME'

BEGIN;

SET search_path TO dma, public;

-- --------------------------------------------------------------------------
-- Schema access
-- --------------------------------------------------------------------------

GRANT USAGE ON SCHEMA dma TO :"app_principal";


-- --------------------------------------------------------------------------
-- Business tables: full read/write for the application
-- --------------------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE, DELETE ON
    country,
    product_class,
    customer,
    customer_market,
    registration,
    gtin_authorization,
    gtin_predecessor,
    gtin_hold,
    evidence,
    request_form,
    request_form_registration
TO :"app_principal";

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dma TO :"app_principal";


-- --------------------------------------------------------------------------
-- Views and the status function
-- --------------------------------------------------------------------------

GRANT SELECT ON registration_current, gtin_shippable TO :"app_principal";
GRANT EXECUTE ON FUNCTION registration_status(boolean, text, boolean, date, integer)
    TO :"app_principal";


-- --------------------------------------------------------------------------
-- Audit trail: append-only
-- --------------------------------------------------------------------------
--
-- Deliberately no UPDATE and no DELETE. Nothing gates a change in this system,
-- so the audit trail is the only record of who changed what and why; it must
-- not be editable by the thing doing the changing.

GRANT SELECT, INSERT ON audit_log TO :"app_principal";
GRANT USAGE, SELECT ON SEQUENCE audit_log_id_seq TO :"app_principal";

REVOKE UPDATE, DELETE ON audit_log FROM :"app_principal";

-- Belt and braces: a rule that rejects any UPDATE or DELETE reaching the table,
-- whatever role attempts it.
CREATE OR REPLACE RULE audit_log_no_update AS
    ON UPDATE TO audit_log DO INSTEAD NOTHING;

CREATE OR REPLACE RULE audit_log_no_delete AS
    ON DELETE TO audit_log DO INSTEAD NOTHING;


-- --------------------------------------------------------------------------
-- Future objects
-- --------------------------------------------------------------------------

ALTER DEFAULT PRIVILEGES IN SCHEMA dma
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_principal";

ALTER DEFAULT PRIVILEGES IN SCHEMA dma
    GRANT USAGE, SELECT ON SEQUENCES TO :"app_principal";

COMMIT;
