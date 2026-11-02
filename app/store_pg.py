"""Lakebase (Postgres) implementation of the Store protocol.

Connection is lazy: the pool is created on first use, not at import or startup.
A deployed app with an unreachable database should start and report unhealthy
rather than crash-loop, and the readiness of Lakebase should not decide whether
the container comes up.

NOT yet exercised against a live database. The SQL parses (pglast) and the
route behaviour is covered by tests against a fake store, but nothing here has
round-tripped through a real Lakebase instance.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .audit import AuditRecord

log = logging.getLogger(__name__)

# Columns returned for a registration row. Explicit, so a schema change cannot
# silently start leaking new columns to the browser.
_REGISTRATION_COLUMNS = """
    id, registration_number, country_code, mah_customer_id, sku,
    product_description, product_class_code, legal_manufacturer, record_type,
    device_class, version, submission_number, submitted_date, approved_date,
    expiry_date, pending_type, in_progress, alert_months, ifu_version,
    company_contact, local_rep, local_distributors, issuing_body, device_udi,
    part_numbers, configuration, medical_code, comments, other_information,
    archived, source_form_id, created_at, created_by, updated_at, updated_by,
    status, alert_opens_on, in_alert_window
"""

# Prefixed form, for the queries that join country. A country code is not a
# thing anyone recognises on a screen.
_REGISTRATION_SELECT = (
    ", ".join("rc." + c.strip() for c in _REGISTRATION_COLUMNS.split(",")) +
    ", c.name AS country_name, c.alpha2 AS country_alpha2"
)


class PostgresStore:
    def __init__(self, database_url: str, pool=None):
        self._url = database_url
        self._pool = pool

    # -- connection -------------------------------------------------------

    def _get_pool(self):
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self._url, min_size=1, max_size=8, open=True, timeout=10
            )
        return self._pool

    def _rows(self, cursor) -> List[Dict[str, Any]]:
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # -- registrations ----------------------------------------------------

    def list_registrations(
        self,
        country_code: Optional[str] = None,
        product_class_code: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []

        if not include_archived:
            where.append("rc.archived = false")
        if country_code:
            where.append("rc.country_code = %s")
            params.append(country_code)
        if product_class_code:
            where.append("rc.product_class_code = %s")
            params.append(product_class_code)
        if status:
            where.append("rc.status = %s")
            params.append(status)
        if search:
            where.append(
                "(rc.registration_number ILIKE %s OR rc.sku ILIKE %s "
                " OR rc.product_description ILIKE %s)"
            )
            like = "%" + search + "%"
            params.extend([like, like, like])

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT " + _REGISTRATION_SELECT +
            " FROM dma.registration_current rc"
            " JOIN dma.country c ON c.code = rc.country_code" + clause +
            " ORDER BY rc.expiry_date NULLS LAST, rc.registration_number LIMIT %s"
        )
        params.append(limit)

        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return self._rows(cur)

    def get_registration(self, registration_id: int) -> Optional[Dict[str, Any]]:
        sql = (
            "SELECT " + _REGISTRATION_SELECT +
            " FROM dma.registration_current rc"
            " JOIN dma.country c ON c.code = rc.country_code WHERE rc.id = %s"
        )
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [registration_id])
            rows = self._rows(cur)
            return rows[0] if rows else None

    def update_registration(
        self,
        registration_id: int,
        changes: Mapping[str, Any],
        actor: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        """The row and its audit rows move together or not at all.

        If the audit insert fails, the change is rolled back with it. A change
        that is not accounted for must not survive.
        """
        if not changes:
            return self.get_registration(registration_id)

        columns = list(changes.keys())
        assignments = ", ".join("%s = %%s" % c for c in columns)
        params = [changes[c] for c in columns]
        params.extend([actor, registration_id])

        sql = (
            "UPDATE dma.registration SET " + assignments +
            ", updated_at = now(), updated_by = %s WHERE id = %s"
        )

        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.rowcount == 0:
                    raise LookupError("No registration with id %s" % registration_id)
                self._insert_audit(cur, records)
        return self.get_registration(registration_id)

    # -- GTIN authorizations ---------------------------------------------

    def list_gtins(
        self, gtin: Optional[str] = None, country_code: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []
        if gtin:
            where.append("g.gtin = %s")
            params.append(gtin)
        if country_code:
            where.append("g.country_code = %s")
            params.append(country_code)
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        sql = (
            "SELECT g.id, g.gtin, g.sku, g.item_description, g.is_kit, "
            "       g.country_code, g.status_for_sale, g.shippable, "
            "       g.reason_comments, g.license_number, g.device_class, "
            "       g.effective_date, g.expiry_date, g.vendor, g.source_system, "
            "       s.on_hold, s.is_shippable "
            "FROM dma.gtin_authorization g "
            "JOIN dma.gtin_shippable s "
            "  ON s.gtin = g.gtin AND s.country_code = g.country_code"
            + clause + " ORDER BY g.gtin, g.country_code LIMIT 1000"
        )
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return self._rows(cur)

    # -- holds ------------------------------------------------------------

    def apply_hold(
        self,
        gtin: str,
        scope: Optional[str],
        reason: str,
        source_ref: Optional[str],
        actor: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        sql = (
            "INSERT INTO dma.gtin_hold (gtin, scope, reason, source_ref, applied_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "RETURNING id, gtin, scope, reason, source_ref, applied_at, applied_by, "
            "          lifted_at, lifted_by, lift_reason"
        )
        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [gtin, scope, reason, source_ref, actor])
                row = self._rows(cur)[0]
                self._insert_audit(cur, records)
        return row

    def lift_hold(
        self,
        hold_id: int,
        actor: str,
        lift_reason: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        sql = (
            "UPDATE dma.gtin_hold "
            "SET lifted_at = now(), lifted_by = %s, lift_reason = %s "
            "WHERE id = %s AND lifted_at IS NULL "
            "RETURNING id, gtin, scope, reason, source_ref, applied_at, applied_by, "
            "          lifted_at, lifted_by, lift_reason"
        )
        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, [actor, lift_reason, hold_id])
                rows = self._rows(cur)
                if not rows:
                    raise LookupError(
                        "Hold %s does not exist or has already been lifted." % hold_id
                    )
                self._insert_audit(cur, records)
        return rows[0]

    def list_holds(
        self, active_only: bool = True, limit: int = 500
    ) -> List[Dict[str, Any]]:
        clause = " WHERE lifted_at IS NULL" if active_only else ""
        sql = (
            "SELECT id, gtin, scope, reason, source_ref, applied_at, applied_by, "
            "       lifted_at, lifted_by, lift_reason "
            "FROM dma.gtin_hold" + clause +
            " ORDER BY applied_at DESC, id DESC LIMIT %s"
        )
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [limit])
            return self._rows(cur)

    # -- request form intake ----------------------------------------------

    def find_country_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Forms give a country name, not a code. Match case-insensitively."""
        if not name:
            return None
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT code, alpha2, name FROM dma.country "
                "WHERE lower(name) = lower(%s) LIMIT 1",
                [name.strip()],
            )
            rows = self._rows(cur)
            return rows[0] if rows else None

    _FORM_COLUMNS = """
        id, filename, volume_path, uploaded_at, uploaded_by, country_name,
        country_code, distributor, ra_contact, request_date, licences_expected,
        signed_by, signature_date, sku_count, source_sheet, parsed, warnings,
        state, reviewed_at, reviewed_by, review_note
    """

    def create_request_form(
        self, fields: Mapping[str, Any], actor: str, records: Sequence[AuditRecord]
    ) -> Dict[str, Any]:
        columns = list(fields.keys())
        placeholders = ", ".join(["%s"] * len(columns))
        sql = (
            "INSERT INTO dma.request_form (" + ", ".join(columns) + ", uploaded_by) "
            "VALUES (" + placeholders + ", %s) RETURNING " + self._FORM_COLUMNS
        )
        params = [fields[c] for c in columns]
        params.append(actor)

        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = self._rows(cur)[0]
                # the row id only exists now, so stamp it onto the audit records
                stamped = [
                    replace(record, entity_id=str(row["id"])) for record in records
                ]
                self._insert_audit(cur, stamped)
        return row

    def get_request_form(self, form_id: int) -> Optional[Dict[str, Any]]:
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT " + self._FORM_COLUMNS + " FROM dma.request_form WHERE id = %s",
                [form_id],
            )
            rows = self._rows(cur)
            return rows[0] if rows else None

    def list_request_forms(
        self, state: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        clause = " WHERE state = %s" if state else ""
        params: List[Any] = [state] if state else []
        params.append(limit)
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT " + self._FORM_COLUMNS + " FROM dma.request_form" + clause +
                " ORDER BY uploaded_at DESC LIMIT %s",
                params,
            )
            return self._rows(cur)

    def accept_request_form(
        self,
        form_id: int,
        registrations: Sequence[Mapping[str, Any]],
        actor: str,
        review_note: str,
        records: Sequence[AuditRecord],
    ) -> List[Dict[str, Any]]:
        created: List[Dict[str, Any]] = []
        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM dma.request_form WHERE id = %s FOR UPDATE",
                    [form_id],
                )
                rows = self._rows(cur)
                if not rows:
                    raise LookupError("No request form with id %s" % form_id)
                if rows[0]["state"] != "parsed":
                    # guards a double click, and a second accept that would
                    # silently duplicate every registration on the form
                    raise ValueError(
                        "This form has already been %s." % rows[0]["state"]
                    )

                for payload in registrations:
                    columns = list(payload.keys())
                    sql = (
                        "INSERT INTO dma.registration (" + ", ".join(columns) +
                        ", source_form_id, created_by, updated_by) VALUES (" +
                        ", ".join(["%s"] * len(columns)) + ", %s, %s, %s) RETURNING id"
                    )
                    params = [payload[c] for c in columns]
                    params.extend([form_id, actor, actor])
                    cur.execute(sql, params)
                    new_id = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO dma.request_form_registration "
                        "(request_form_id, registration_id, licence_group) "
                        "VALUES (%s, %s, %s)",
                        [form_id, new_id, payload.get("_licence_group", 0)],
                    )
                    created.append({"id": new_id})

                cur.execute(
                    "UPDATE dma.request_form SET state = 'accepted', "
                    "reviewed_at = now(), reviewed_by = %s, review_note = %s "
                    "WHERE id = %s",
                    [actor, review_note, form_id],
                )
                self._insert_audit(cur, records)

        return [self.get_registration(c["id"]) for c in created]

    def reject_request_form(
        self, form_id: int, actor: str, review_note: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        with self._get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE dma.request_form SET state = 'rejected', "
                    "reviewed_at = now(), reviewed_by = %s, review_note = %s "
                    "WHERE id = %s AND state = 'parsed'",
                    [actor, review_note, form_id],
                )
                if cur.rowcount == 0:
                    raise ValueError(
                        "Form %s does not exist or has already been reviewed." % form_id
                    )
                self._insert_audit(cur, records)
        return self.get_request_form(form_id)

    # -- audit ------------------------------------------------------------

    def _insert_audit(self, cursor, records: Sequence[AuditRecord]) -> None:
        if not records:
            return
        cursor.executemany(
            "INSERT INTO dma.audit_log "
            "(occurred_at, actor, action, entity, entity_id, field, "
            " old_value, new_value, reason, request_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    r.occurred_at, r.actor, r.action, r.entity, r.entity_id,
                    r.field, r.old_value, r.new_value, r.reason, r.request_id,
                )
                for r in records
            ],
        )

    def write_audit(self, records: Sequence[AuditRecord]) -> None:
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            self._insert_audit(cur, records)

    def audit_for(
        self, entity: str, entity_id: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT occurred_at, actor, action, field, old_value, new_value, "
            "       reason, request_id "
            "FROM dma.audit_log WHERE entity = %s AND entity_id = %s "
            "ORDER BY occurred_at DESC, id DESC LIMIT %s"
        )
        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [entity, entity_id, limit])
            return self._rows(cur)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
