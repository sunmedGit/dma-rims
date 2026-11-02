-- dma-rims — Unity Catalog grants
--
-- DRAFT — requires QA/RA review. Run by someone who administers cdw_prod.
--
-- Unity Catalog grants to a service principal use its application id, not
-- its display name. The app's principal is "app-1pcqpc dma-rims":
--
--     application id 573ad41d-90e0-4a4e-9d7c-9b77086d9f55
--
-- Editor / Viewer is enforced in the application, not here. The database sees
-- one principal — the app — for every user.
--
-- What this cannot do that sql/grants.sql did on Postgres: grant INSERT
-- without UPDATE and DELETE. MODIFY is all three. The audit log and export
-- history are append-only by the application's discipline, not by grant.


-- ---------------------------------------------------------------------------
-- The application
-- ---------------------------------------------------------------------------

GRANT USE CATALOG ON CATALOG cdw_prod                TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT USE SCHEMA  ON SCHEMA  cdw_prod.glb_bi         TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;

GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_product_class             TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_customer                  TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_customer_market           TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_registration              TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_gtin_authorization        TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_gtin_predecessor          TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_gtin_hold                 TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_evidence                  TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_request_form              TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_request_form_registration TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
-- MODIFY here is wider than the app needs: it only ever inserts.
GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_audit_log                 TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;

GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_country                           TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_registration_current              TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_gtin_country_eligibility       TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_gtin_country_blocked           TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_gtin_country_expiring          TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_export_run_log                 TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;

GRANT EXECUTE ON FUNCTION cdw_prod.glb_bi.dma_registration_status           TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;
GRANT EXECUTE ON FUNCTION cdw_prod.glb_bi.dma_eligibility_as_of             TO `573ad41d-90e0-4a4e-9d7c-9b77086d9f55`;


-- ---------------------------------------------------------------------------
-- The nightly job
-- ---------------------------------------------------------------------------
--
-- If the job runs as its own principal rather than as a person, it needs to
-- read the views and write only the two export tables — nothing the app owns.
-- Replace the placeholder with that principal's application id.
--
-- GRANT USE CATALOG ON CATALOG cdw_prod        TO `REPLACE_JOB_PRINCIPAL`;
-- GRANT USE SCHEMA  ON SCHEMA  cdw_prod.glb_bi TO `REPLACE_JOB_PRINCIPAL`;
-- GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_gtin_country_eligibility TO `REPLACE_JOB_PRINCIPAL`;
-- GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_gtin_country_blocked     TO `REPLACE_JOB_PRINCIPAL`;
-- GRANT SELECT ON TABLE cdw_prod.glb_bi.dma_vw_gtin_country_expiring    TO `REPLACE_JOB_PRINCIPAL`;
-- GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_export_run                 TO `REPLACE_JOB_PRINCIPAL`;
-- GRANT SELECT, MODIFY ON TABLE cdw_prod.glb_bi.dma_export_eligibility_history TO `REPLACE_JOB_PRINCIPAL`;
