-- dma-rims — tables on Unity Catalog / Delta, in cdw_prod.glb_bi
--
-- DRAFT — requires QA/RA review. Nothing here is validated.
--
-- Run by someone with rights on cdw_prod. I have not executed any of this:
-- I can read that schema but do not create objects in production. Run it in
-- cdw_dev first if you want it proven before it touches prod.
--
-- This replaces sql/schema.sql, which targets Lakebase (Postgres). Keep that
-- file: if this ever moves to Lakebase, it is the better schema, for the
-- reasons listed under "What Delta cannot enforce" below.
--
-- Naming: glb_bi is a shared flat schema that separates applications by
-- prefix (cm_, eifu_, gpo_, sp_). These follow that convention with dma_.
--
-- ---------------------------------------------------------------------------
-- What Delta cannot enforce, that Postgres did
-- ---------------------------------------------------------------------------
--
--   1. UNIQUENESS. Delta has no unique indexes and PRIMARY KEY is
--      informational. Nothing stops two registrations sharing a certificate
--      number in one country, or two rows for the same GTIN x country. The
--      application checks instead, which means the check is racy under
--      concurrent writes. With this user population that is a small risk, but
--      it is a real one and it is not mitigated here.
--
--   2. APPEND-ONLY. Unity Catalog's table privilege is MODIFY, which grants
--      INSERT, UPDATE and DELETE together. The audit trail cannot be made
--      insert-only by withholding a grant the way it is on Postgres. Its
--      immutability now rests on the application never issuing an UPDATE or
--      DELETE against it, with Delta history as the forensic backstop.
--      Confirm this reading of the UC privilege model with whoever
--      administers it before relying on it in a quality context.
--
--   3. FOREIGN KEYS. Declared below as NOT ENFORCED, so they document intent
--      and help the optimizer, but a registration can reference a country
--      that does not exist.
--
-- CHECK constraints ARE enforced, so the value-domain rules survive intact.
--
-- No column DEFAULTs are declared. Delta supports them only with an extra
-- table feature enabled, and the application supplies created_at/created_by
-- on every insert anyway. One less thing to go wrong on the first run.


-- ---------------------------------------------------------------------------
-- Reference
-- ---------------------------------------------------------------------------

-- Countries are NOT duplicated. cm_country_codes already carries the codes GP
-- uses, and GP is where the downstream consumers (Salespad, Korber) live, so
-- sourcing from it means no translation layer between this app and them.
-- The source column names are literal SQL text from an unaliased CTAS, hence
-- the backticks.
CREATE OR REPLACE VIEW cdw_prod.glb_bi.dma_country AS
SELECT
    `trim(CCode)`      AS code,      -- GP country code, e.g. 'MY'
    `TRIM(CCodeDesc)`  AS name,      -- e.g. 'MALAYSIA'
    `TRIM(ISOCode)`    AS iso_alpha3 -- e.g. 'MYS'; blank for non-ISO entries
FROM cdw_prod.glb_bi.cm_country_codes
WHERE `trim(CCode)` IS NOT NULL;

COMMENT ON VIEW cdw_prod.glb_bi.dma_country IS
  'Country reference for dma-rims, sourced from cm_country_codes so GP and this application agree on codes. Not strictly ISO: some entries (e.g. IC, Canary Islands) are GP-specific.';


CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_product_class (
    code        STRING NOT NULL COMMENT 'AIRWAYS, BP CUFFS, ETCO2, ...',
    label       STRING NOT NULL,
    sort_order  INT    NOT NULL
) USING DELTA
COMMENT 'Commercial product family. NOT the regulatory device class (I/IIa/III), which lives on each registration. Placeholder list pending the real taxonomy and the system that owns the product hierarchy.';


-- ---------------------------------------------------------------------------
-- Customers / Market Authorization Holders
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_customer (
    id               BIGINT GENERATED ALWAYS AS IDENTITY,
    slug             STRING NOT NULL,
    name             STRING NOT NULL,
    is_airlife       BOOLEAN NOT NULL,
    sap_id           STRING COMMENT 'Salespad / GP account id. One per customer; a customer holding several account ids across markets would make this a child table.',
    ra_contact_role  STRING,
    ra_contact_email STRING,
    loa_on_file      BOOLEAN NOT NULL,
    loa_expiry       DATE,
    created_at       TIMESTAMP NOT NULL,
    updated_at       TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_customer_market (
    customer_id  BIGINT NOT NULL,
    country_code STRING NOT NULL
) USING DELTA;


-- ---------------------------------------------------------------------------
-- Country registrations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_registration (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY,
    registration_number STRING COMMENT 'NULL until the authority issues one. A registration raised from a request form has no certificate yet, and a placeholder that looks like a certificate number invites someone to treat it as one.',
    country_code        STRING NOT NULL,
    mah_customer_id     BIGINT,
    sku                 STRING,
    product_description STRING,
    product_class_code  STRING,

    legal_manufacturer  STRING COMMENT 'The AirLife legal entity',
    record_type         STRING,
    device_class        STRING COMMENT 'Regulatory class: I/IIa/III/A-D',
    version             STRING,
    submission_number   STRING,

    submitted_date      DATE,
    approved_date       DATE,
    expiry_date         DATE,
    pending_type        STRING,
    in_progress         BOOLEAN NOT NULL,

    alert_months        INT NOT NULL,
    ifu_version         STRING,
    company_contact     STRING,
    local_rep           STRING,
    local_distributors  STRING,
    issuing_body        STRING,
    device_udi          STRING,
    part_numbers        STRING,
    configuration       STRING,
    medical_code        STRING,
    comments            STRING,
    other_information   STRING,

    archived            BOOLEAN NOT NULL,
    source_form_id      BIGINT COMMENT 'Set when raised from a request form',
    created_at          TIMESTAMP NOT NULL,
    created_by          STRING NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    updated_by          STRING NOT NULL
) USING DELTA
-- Delta has no indexes. Clustering on the columns the app filters by is the
-- nearest equivalent and lets file skipping do the work.
CLUSTER BY (country_code, expiry_date);

ALTER TABLE cdw_prod.glb_bi.dma_registration
    ADD CONSTRAINT dma_registration_pending_type_ck
    CHECK (pending_type IS NULL
           OR pending_type IN ('Initial Registration', 'Modification', 'Renewal'));

ALTER TABLE cdw_prod.glb_bi.dma_registration
    ADD CONSTRAINT dma_registration_alert_months_ck
    CHECK (alert_months BETWEEN 1 AND 48);


-- ---------------------------------------------------------------------------
-- GTIN authorization
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_gtin_authorization (
    id               BIGINT GENERATED ALWAYS AS IDENTITY,
    gtin             STRING NOT NULL,
    sku              STRING,
    item_description STRING,
    is_kit           BOOLEAN NOT NULL,
    country_code     STRING NOT NULL,

    status_for_sale  STRING NOT NULL,
    shippable        BOOLEAN NOT NULL,
    reason_comments  STRING,

    license_number   STRING,
    device_class     STRING,
    effective_date   DATE,
    expiry_date      DATE,
    vendor           STRING,
    source_system    STRING COMMENT 'GP / MDM / manual',
    registration_id  BIGINT,

    created_at       TIMESTAMP NOT NULL,
    created_by       STRING NOT NULL,
    updated_at       TIMESTAMP NOT NULL,
    updated_by       STRING NOT NULL
) USING DELTA
CLUSTER BY (gtin, country_code);

ALTER TABLE cdw_prod.glb_bi.dma_gtin_authorization
    ADD CONSTRAINT dma_gtin_status_for_sale_ck
    CHECK (status_for_sale IN ('Approved', 'Not Approved', 'Pending'));

-- NOTE: (gtin, country_code) was UNIQUE on Postgres and cannot be here.
-- The application must not create a second row for a pair that already exists,
-- and the nightly job should alert if this returns anything:
--   SELECT gtin, country_code, count(*) c
--   FROM cdw_prod.glb_bi.dma_gtin_authorization
--   GROUP BY gtin, country_code HAVING count(*) > 1;

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_gtin_predecessor (
    gtin     STRING NOT NULL,
    old_gtin STRING NOT NULL
) USING DELTA
COMMENT 'Superseded GTINs, so a transition can be seen rather than inferred.';


-- ---------------------------------------------------------------------------
-- Holds, as events
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_gtin_hold (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    gtin        STRING NOT NULL,
    scope       STRING COMMENT 'Country code, or NULL for a global hold',
    reason      STRING NOT NULL,
    source_ref  STRING COMMENT 'CAPA / DCR / NCR reference',
    applied_at  TIMESTAMP NOT NULL,
    applied_by  STRING NOT NULL,
    lifted_at   TIMESTAMP,
    lifted_by   STRING,
    lift_reason STRING
) USING DELTA
CLUSTER BY (gtin);

ALTER TABLE cdw_prod.glb_bi.dma_gtin_hold
    ADD CONSTRAINT dma_gtin_hold_lifted_after_applied_ck
    CHECK (lifted_at IS NULL OR lifted_at >= applied_at);


-- ---------------------------------------------------------------------------
-- Approval evidence (metadata; files live in a Unity Catalog Volume)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_evidence (
    id              BIGINT GENERATED ALWAYS AS IDENTITY,
    scope           STRING NOT NULL,
    registration_id BIGINT,
    customer_id     BIGINT,

    filename        STRING NOT NULL,
    content_type    STRING,
    size_bytes      BIGINT NOT NULL,
    volume_path     STRING NOT NULL,

    country_code    STRING COMMENT 'What the evidence covers; NULL means all',
    product_sku     STRING,

    uploaded_at     TIMESTAMP NOT NULL,
    uploaded_by     STRING NOT NULL
) USING DELTA;

ALTER TABLE cdw_prod.glb_bi.dma_evidence
    ADD CONSTRAINT dma_evidence_scope_ck
    CHECK (
        (scope = 'registration' AND registration_id IS NOT NULL AND customer_id IS NULL)
     OR (scope = 'customer'     AND customer_id     IS NOT NULL AND registration_id IS NULL)
    );


-- ---------------------------------------------------------------------------
-- Registration Request Form intake
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_request_form (
    id                BIGINT GENERATED ALWAYS AS IDENTITY,
    filename          STRING NOT NULL,
    volume_path       STRING NOT NULL,
    uploaded_at       TIMESTAMP NOT NULL,
    uploaded_by       STRING NOT NULL,

    country_name      STRING,
    country_code      STRING,
    distributor       STRING,
    ra_contact        STRING,
    request_date      DATE,
    licences_expected INT,
    signed_by         STRING,
    signature_date    DATE,
    sku_count         INT,
    source_sheet      STRING,

    parsed            VARIANT NOT NULL COMMENT 'Full parser output',
    warnings          VARIANT NOT NULL COMMENT 'Parser warnings a human must clear before the form is accepted: ambiguous licence grouping, unsigned form, licence count disagreeing with the notes.',

    state             STRING NOT NULL,
    reviewed_at       TIMESTAMP,
    reviewed_by       STRING,
    review_note       STRING
) USING DELTA;

ALTER TABLE cdw_prod.glb_bi.dma_request_form
    ADD CONSTRAINT dma_request_form_state_ck
    CHECK (state IN ('parsed', 'accepted', 'rejected'));

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_request_form_registration (
    request_form_id BIGINT NOT NULL,
    registration_id BIGINT NOT NULL,
    licence_group   INT NOT NULL
) USING DELTA
COMMENT 'Which registrations a given form produced.';


-- ---------------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------------
--
-- Nothing gates a change in this system, so this table is the only record of
-- who changed what and why. One row per changed field.
--
-- Append-only by application discipline, NOT by grant — see the header. Delta
-- history is the backstop: DESCRIBE HISTORY will show an UPDATE or DELETE
-- against this table even if the rows no longer show it.

CREATE TABLE IF NOT EXISTS cdw_prod.glb_bi.dma_audit_log (
    id          BIGINT GENERATED ALWAYS AS IDENTITY,
    occurred_at TIMESTAMP NOT NULL,
    actor       STRING NOT NULL COMMENT 'Workspace identity, never app-local',
    action      STRING NOT NULL,
    entity      STRING NOT NULL COMMENT 'Table name, without the dma_ prefix',
    entity_id   STRING NOT NULL,
    field       STRING COMMENT 'NULL for insert/delete',
    old_value   STRING,
    new_value   STRING,
    reason      STRING NOT NULL COMMENT 'Required: why this changed',
    request_id  STRING COMMENT 'Ties one user action together'
) USING DELTA
CLUSTER BY (entity, occurred_at);

ALTER TABLE cdw_prod.glb_bi.dma_audit_log
    ADD CONSTRAINT dma_audit_log_action_ck
    CHECK (action IN ('insert', 'update', 'delete'));

COMMENT ON TABLE cdw_prod.glb_bi.dma_audit_log IS
  'Append-only. The application must never issue UPDATE or DELETE against this table. Unity Catalog cannot enforce that by grant, so any such operation is a defect, visible in DESCRIBE HISTORY.';


-- ---------------------------------------------------------------------------
-- Informational keys
-- ---------------------------------------------------------------------------
-- Not enforced. They document intent and inform the optimizer; they do not
-- prevent an orphan.

ALTER TABLE cdw_prod.glb_bi.dma_product_class
    ADD CONSTRAINT dma_product_class_pk PRIMARY KEY (code);
ALTER TABLE cdw_prod.glb_bi.dma_customer
    ADD CONSTRAINT dma_customer_pk PRIMARY KEY (id);
ALTER TABLE cdw_prod.glb_bi.dma_registration
    ADD CONSTRAINT dma_registration_pk PRIMARY KEY (id);
ALTER TABLE cdw_prod.glb_bi.dma_gtin_authorization
    ADD CONSTRAINT dma_gtin_authorization_pk PRIMARY KEY (id);
ALTER TABLE cdw_prod.glb_bi.dma_gtin_hold
    ADD CONSTRAINT dma_gtin_hold_pk PRIMARY KEY (id);
ALTER TABLE cdw_prod.glb_bi.dma_evidence
    ADD CONSTRAINT dma_evidence_pk PRIMARY KEY (id);
ALTER TABLE cdw_prod.glb_bi.dma_request_form
    ADD CONSTRAINT dma_request_form_pk PRIMARY KEY (id);
ALTER TABLE cdw_prod.glb_bi.dma_audit_log
    ADD CONSTRAINT dma_audit_log_pk PRIMARY KEY (id);

ALTER TABLE cdw_prod.glb_bi.dma_registration
    ADD CONSTRAINT dma_registration_customer_fk
    FOREIGN KEY (mah_customer_id) REFERENCES cdw_prod.glb_bi.dma_customer(id)
    NOT ENFORCED;
ALTER TABLE cdw_prod.glb_bi.dma_registration
    ADD CONSTRAINT dma_registration_product_class_fk
    FOREIGN KEY (product_class_code) REFERENCES cdw_prod.glb_bi.dma_product_class(code)
    NOT ENFORCED;
ALTER TABLE cdw_prod.glb_bi.dma_registration
    ADD CONSTRAINT dma_registration_source_form_fk
    FOREIGN KEY (source_form_id) REFERENCES cdw_prod.glb_bi.dma_request_form(id)
    NOT ENFORCED;
ALTER TABLE cdw_prod.glb_bi.dma_gtin_authorization
    ADD CONSTRAINT dma_gtin_registration_fk
    FOREIGN KEY (registration_id) REFERENCES cdw_prod.glb_bi.dma_registration(id)
    NOT ENFORCED;
