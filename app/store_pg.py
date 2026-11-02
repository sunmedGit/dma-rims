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
            where.append("archived = false")
        if country_code:
            where.append("country_code = %s")
            params.append(country_code)
        if product_class_code:
            where.append("product_class_code = %s")
            params.append(product_class_code)
        if status:
            where.append("status = %s")
            params.append(status)
        if search:
            where.append(
                "(registration_number ILIKE %s OR sku ILIKE %s "
                " OR product_description ILIKE %s)"
            )
            like = "%" + search + "%"
            params.extend([like, like, like])

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT " + _REGISTRATION_COLUMNS +
            " FROM dma.registration_current" + clause +
            " ORDER BY expiry_date NULLS LAST, registration_number LIMIT %s"
        )
        params.append(limit)

        with self._get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return self._rows(cur)

    def get_registration(self, registration_id: int) -> Optional[Dict[str, Any]]:
        sql = (
            "SELECT " + _REGISTRATION_COLUMNS +
            " FROM dma.registration_current WHERE id = %s"
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
