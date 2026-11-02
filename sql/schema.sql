-- dma-rims — Lakebase (Postgres) schema
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- Run by someone with rights on the target database. This repository does not
-- create Databricks objects; see README.
--
-- Two rules this schema enforces rather than leaves to the application:
--
--   1. Registration status is NEVER stored. It is derived from the expiry date
--      against today, every time it is read. A stored status goes stale the day
--      a licence lapses and then reads as active, which is the one failure this
--      system exists to prevent.
--
--   2. Shipping eligibility accounts for expiry. The prototype's "shippable"
--      calculation checked approval, blocking and holds but not expiry, so an
--      expired licence still read as shippable. If this data ever feeds
--      Salespad/Korber inventory suppression, that gap releases product on a
--      lapsed registration. See view gtin_shippable below.

BEGIN;

CREATE SCHEMA IF NOT EXISTS dma;
SET search_path TO dma, public;


-- ---------------------------------------------------------------------------
-- Reference
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS country (
    code            text PRIMARY KEY,              -- ISO 3166-1 numeric, e.g. '458'
    alpha2          text NOT NULL,
    name            text NOT NULL
);

CREATE TABLE IF NOT EXISTS product_class (
    code            text PRIMARY KEY,              -- 'AIRWAYS', 'BP CUFFS', 'ETCO2', ...
    label           text NOT NULL,
    sort_order      integer NOT NULL DEFAULT 100
);

COMMENT ON TABLE product_class IS
  'Commercial product family. NOT the regulatory device class (I/IIa/III), which '
  'lives on each registration. Placeholder list pending the real taxonomy and '
  'the system that owns the product hierarchy.';


-- ---------------------------------------------------------------------------
-- Customers / Market Authorization Holders
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS customer (
    id              bigserial PRIMARY KEY,
    slug            text NOT NULL UNIQUE,
    name            text NOT NULL,
    is_airlife      boolean NOT NULL DEFAULT false,
    sap_id          text,                          -- Salespad / SAP account id
    ra_contact_role text,
    ra_contact_email text,
    loa_on_file     boolean NOT NULL DEFAULT false,
    loa_expiry      date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON COLUMN customer.sap_id IS
  'One id per customer. Open question: a customer may hold several account ids '
  'across markets, which would make this a child table.';

CREATE TABLE IF NOT EXISTS customer_market (
    customer_id     bigint NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
    country_code    text NOT NULL REFERENCES country(code),
    PRIMARY KEY (customer_id, country_code)
);


-- ---------------------------------------------------------------------------
-- Country registrations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS registration (
    id                  bigserial PRIMARY KEY,
    -- NULL until the authority issues one. A registration raised from a request
    -- form has no certificate yet, and inventing a placeholder that looks like a
    -- certificate number invites someone to treat it as one.
    registration_number text,
    country_code        text NOT NULL REFERENCES country(code),
    mah_customer_id     bigint REFERENCES customer(id),
    sku                 text,
    product_description text,
    product_class_code  text REFERENCES product_class(code),

    legal_manufacturer  text,                      -- the AirLife legal entity
    record_type         text,
    device_class        text,                      -- regulatory class: I/IIa/III/A-D
    version             text,
    submission_number   text,

    submitted_date      date,
    approved_date       date,
    expiry_date         date,
    pending_type        text
        CHECK (pending_type IS NULL OR pending_type IN
               ('Initial Registration', 'Modification', 'Renewal')),
    in_progress         boolean NOT NULL DEFAULT false,

    alert_months        integer NOT NULL DEFAULT 12
        CHECK (alert_months BETWEEN 1 AND 48),
    ifu_version         text,
    company_contact     text,
    local_rep           text,
    local_distributors  text,
    issuing_body        text,
    device_udi          text,
    part_numbers        text,
    configuration       text,
    medical_code        text,
    comments            text,
    other_information   text,

    archived            boolean NOT NULL DEFAULT false,
    source_form_id      bigint,                    -- set when raised from a form
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text NOT NULL
);

-- Certificate numbers are unique per country once they exist. Pending records
-- carry no number, and any number of them may be open for the same country.
CREATE UNIQUE INDEX IF NOT EXISTS registration_number_country_uq
    ON registration (registration_number, country_code)
    WHERE registration_number IS NOT NULL;

CREATE INDEX IF NOT EXISTS registration_country_idx ON registration (country_code);
CREATE INDEX IF NOT EXISTS registration_expiry_idx  ON registration (expiry_date)
    WHERE archived = false;
CREATE INDEX IF NOT EXISTS registration_mah_idx     ON registration (mah_customer_id);
CREATE INDEX IF NOT EXISTS registration_sku_idx     ON registration (sku);


-- Status is derived, never stored. One definition, used by SQL and the API.
CREATE OR REPLACE FUNCTION registration_status(
    p_archived boolean,
    p_pending_type text,
    p_in_progress boolean,
    p_expiry_date date,
    p_expiring_days integer DEFAULT 90
) RETURNS text
-- STABLE, not IMMUTABLE: the result depends on current_date, so it changes
-- between transactions. Marking it IMMUTABLE would let the planner cache a
-- result across days and an expired licence would keep reading as active.
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_archived                     THEN 'Archived'
        WHEN p_pending_type IS NOT NULL     THEN 'Pending'
        WHEN p_in_progress                  THEN 'In progress'
        WHEN p_expiry_date IS NULL          THEN 'Active'
        WHEN p_expiry_date <  current_date  THEN 'Expired'
        WHEN p_expiry_date <= current_date + make_interval(days => p_expiring_days)
                                            THEN 'Expiring soon'
        ELSE 'Active'
    END;
$$;

CREATE OR REPLACE VIEW registration_current AS
SELECT
    r.*,
    registration_status(r.archived, r.pending_type, r.in_progress, r.expiry_date)
        AS status,
    -- the alert window opens alert_months before expiry
    CASE WHEN r.expiry_date IS NULL THEN NULL
         ELSE r.expiry_date - (r.alert_months * 30.44)::integer
    END AS alert_opens_on,
    CASE WHEN r.expiry_date IS NULL
              OR r.pending_type IS NOT NULL
              OR r.in_progress THEN false
         ELSE current_date >= r.expiry_date - (r.alert_months * 30.44)::integer
              AND r.expiry_date >= current_date
    END AS in_alert_window
FROM registration r;


-- ---------------------------------------------------------------------------
-- GTIN authorization and shipping eligibility
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gtin_authorization (
    id                  bigserial PRIMARY KEY,
    gtin                text NOT NULL,
    sku                 text,
    item_description    text,
    is_kit              boolean NOT NULL DEFAULT false,
    country_code        text NOT NULL REFERENCES country(code),

    status_for_sale     text NOT NULL DEFAULT 'Not Approved'
        CHECK (status_for_sale IN ('Approved', 'Not Approved', 'Pending')),
    shippable           boolean NOT NULL DEFAULT false,
    reason_comments     text,

    license_number      text,
    device_class        text,
    effective_date      date,
    expiry_date         date,
    vendor              text,
    source_system       text,                      -- GP / MDM / manual
    registration_id     bigint REFERENCES registration(id),

    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text NOT NULL,

    UNIQUE (gtin, country_code)
);

CREATE INDEX IF NOT EXISTS gtin_auth_gtin_idx ON gtin_authorization (gtin);
CREATE INDEX IF NOT EXISTS gtin_auth_sku_idx  ON gtin_authorization (sku);

-- Superseded GTINs, so a transition can be seen rather than inferred.
CREATE TABLE IF NOT EXISTS gtin_predecessor (
    gtin                text NOT NULL,
    old_gtin            text NOT NULL,
    PRIMARY KEY (gtin, old_gtin)
);


-- ---------------------------------------------------------------------------
-- Holds, as events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS gtin_hold (
    id              bigserial PRIMARY KEY,
    gtin            text NOT NULL,
    scope           text,                          -- country code, or NULL = global
    reason          text NOT NULL,
    source_ref      text,                          -- CAPA / DCR / NCR reference
    applied_at      timestamptz NOT NULL DEFAULT now(),
    applied_by      text NOT NULL,
    lifted_at       timestamptz,
    lifted_by       text,
    lift_reason     text,
    CHECK (lifted_at IS NULL OR lifted_at >= applied_at)
);

CREATE INDEX IF NOT EXISTS gtin_hold_active_idx ON gtin_hold (gtin)
    WHERE lifted_at IS NULL;


-- Shipping eligibility, in one place.
--
-- A GTIN ships into a country only when it is approved, not blocked, not held,
-- and its authorization has not expired. The expiry term is the one the
-- prototype was missing.
CREATE OR REPLACE VIEW gtin_shippable AS
SELECT
    g.gtin,
    g.sku,
    g.country_code,
    g.status_for_sale,
    g.shippable,
    g.expiry_date,
    EXISTS (
        SELECT 1 FROM gtin_hold h
        WHERE h.gtin = g.gtin
          AND h.lifted_at IS NULL
          AND (h.scope IS NULL OR h.scope = g.country_code)
    ) AS on_hold,
    (
        g.status_for_sale = 'Approved'
        AND g.shippable
        AND (g.expiry_date IS NULL OR g.expiry_date >= current_date)
        AND NOT EXISTS (
            SELECT 1 FROM gtin_hold h
            WHERE h.gtin = g.gtin
              AND h.lifted_at IS NULL
              AND (h.scope IS NULL OR h.scope = g.country_code)
        )
    ) AS is_shippable
FROM gtin_authorization g;

COMMENT ON VIEW gtin_shippable IS
  'Single definition of shipping eligibility. If this ever feeds downstream '
  'inventory suppression, this view is the contract.';


-- ---------------------------------------------------------------------------
-- Approval evidence (metadata; the files live in a Unity Catalog Volume)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS evidence (
    id              bigserial PRIMARY KEY,
    scope           text NOT NULL CHECK (scope IN ('registration', 'customer')),
    registration_id bigint REFERENCES registration(id) ON DELETE CASCADE,
    customer_id     bigint REFERENCES customer(id)     ON DELETE CASCADE,

    filename        text NOT NULL,
    content_type    text,
    size_bytes      bigint NOT NULL,
    volume_path     text NOT NULL,                 -- /Volumes/<cat>/<schema>/evidence/...

    -- what the evidence actually covers; NULL means "all"
    country_code    text REFERENCES country(code),
    product_sku     text,

    uploaded_at     timestamptz NOT NULL DEFAULT now(),
    uploaded_by     text NOT NULL,

    CHECK (
        (scope = 'registration' AND registration_id IS NOT NULL AND customer_id IS NULL)
     OR (scope = 'customer'     AND customer_id     IS NOT NULL AND registration_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS evidence_reg_idx  ON evidence (registration_id);
CREATE INDEX IF NOT EXISTS evidence_cust_idx ON evidence (customer_id);


-- ---------------------------------------------------------------------------
-- Registration Request Form intake
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS request_form (
    id                  bigserial PRIMARY KEY,
    filename            text NOT NULL,
    volume_path         text NOT NULL,
    uploaded_at         timestamptz NOT NULL DEFAULT now(),
    uploaded_by         text NOT NULL,

    -- as read from the workbook
    country_name        text,
    country_code        text REFERENCES country(code),
    distributor         text,
    ra_contact          text,
    request_date        date,
    licences_expected   integer,
    signed_by           text,
    signature_date      date,
    sku_count           integer,
    source_sheet        text,

    parsed              jsonb NOT NULL,            -- full parser output
    warnings            jsonb NOT NULL DEFAULT '[]'::jsonb,

    state               text NOT NULL DEFAULT 'parsed'
        CHECK (state IN ('parsed', 'accepted', 'rejected')),
    reviewed_at         timestamptz,
    reviewed_by         text,
    review_note         text
);

COMMENT ON COLUMN request_form.warnings IS
  'Parser warnings a human must clear before the form is accepted — ambiguous '
  'licence grouping, unsigned form, licence count disagreeing with the notes.';

-- Which registrations a given form produced.
CREATE TABLE IF NOT EXISTS request_form_registration (
    request_form_id bigint NOT NULL REFERENCES request_form(id) ON DELETE CASCADE,
    registration_id bigint NOT NULL REFERENCES registration(id) ON DELETE CASCADE,
    licence_group   integer NOT NULL,
    PRIMARY KEY (request_form_id, registration_id)
);

ALTER TABLE registration
    DROP CONSTRAINT IF EXISTS registration_source_form_fk;
ALTER TABLE registration
    ADD CONSTRAINT registration_source_form_fk
    FOREIGN KEY (source_form_id) REFERENCES request_form(id);


-- ---------------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------------
--
-- Nothing gates a change in this system, so this table is the only record of
-- who changed what and why. One row per changed field.

CREATE TABLE IF NOT EXISTS audit_log (
    id              bigserial PRIMARY KEY,
    occurred_at     timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,                 -- workspace identity, never app-local
    action          text NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
    entity          text NOT NULL,                 -- table name
    entity_id       text NOT NULL,
    field           text,                          -- NULL for insert/delete
    old_value       text,
    new_value       text,
    reason          text NOT NULL,                 -- required: why this changed
    request_id      text                           -- ties one user action together
);

CREATE INDEX IF NOT EXISTS audit_entity_idx   ON audit_log (entity, entity_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_actor_idx    ON audit_log (actor, occurred_at DESC);
CREATE INDEX IF NOT EXISTS audit_occurred_idx ON audit_log (occurred_at DESC);

COMMENT ON TABLE audit_log IS
  'Append-only. No UPDATE or DELETE grant should be issued on this table to the '
  'application role.';


-- ---------------------------------------------------------------------------
-- Access
-- ---------------------------------------------------------------------------
--
-- Two levels, mapped from Databricks workspace groups by the application.
-- Create the roles and grant to the app service principal separately; the
-- commands are in sql/grants.sql so they can be reviewed on their own.

COMMIT;
