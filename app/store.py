"""Data access interface.

The routes depend on this protocol rather than on psycopg, so the API can be
tested without a database and the Postgres implementation can be swapped or
mocked. `store_pg.PostgresStore` is the real one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from .audit import AuditRecord

# Columns a caller may change on a registration. Anything outside this list is
# ignored by the diff, so an unexpected key in a payload can neither be written
# nor produce an audit row.
REGISTRATION_EDITABLE = (
    "registration_number",
    "sku",
    "product_description",
    "product_class_code",
    "mah_customer_id",
    "legal_manufacturer",
    "record_type",
    "device_class",
    "version",
    "submission_number",
    "submitted_date",
    "approved_date",
    "expiry_date",
    "pending_type",
    "in_progress",
    "alert_months",
    "ifu_version",
    "company_contact",
    "local_rep",
    "local_distributors",
    "issuing_body",
    "device_udi",
    "part_numbers",
    "configuration",
    "medical_code",
    "comments",
    "other_information",
    "archived",
)

GTIN_EDITABLE = (
    "sku",
    "item_description",
    "is_kit",
    "status_for_sale",
    "shippable",
    "reason_comments",
    "license_number",
    "device_class",
    "effective_date",
    "expiry_date",
    "vendor",
    "source_system",
)


class Store(Protocol):
    # --- registrations ---------------------------------------------------
    def list_registrations(
        self,
        country_code: Optional[str] = None,
        product_class_code: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        include_archived: bool = False,
        limit: int = 250,
    ) -> List[Dict[str, Any]]:
        ...

    def get_registration(self, registration_id: int) -> Optional[Dict[str, Any]]:
        ...

    def update_registration(
        self,
        registration_id: int,
        changes: Mapping[str, Any],
        actor: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        ...

    # --- GTIN authorizations --------------------------------------------
    def list_gtins(
        self, gtin: Optional[str] = None, country_code: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        ...

    # --- holds -----------------------------------------------------------
    def apply_hold(
        self,
        gtin: str,
        scope: Optional[str],
        reason: str,
        source_ref: Optional[str],
        actor: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        ...

    def lift_hold(
        self,
        hold_id: int,
        actor: str,
        lift_reason: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        ...

    def list_holds(self, active_only: bool = True, limit: int = 500
                   ) -> List[Dict[str, Any]]:
        ...

    # --- request form intake --------------------------------------------
    def find_country_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        ...

    def create_request_form(
        self, fields: Mapping[str, Any], actor: str, records: Sequence[AuditRecord]
    ) -> Dict[str, Any]:
        ...

    def get_request_form(self, form_id: int) -> Optional[Dict[str, Any]]:
        ...

    def list_request_forms(self, state: Optional[str] = None, limit: int = 100
                           ) -> List[Dict[str, Any]]:
        ...

    def accept_request_form(
        self,
        form_id: int,
        registrations: Sequence[Mapping[str, Any]],
        actor: str,
        review_note: str,
        records: Sequence[AuditRecord],
    ) -> List[Dict[str, Any]]:
        """Create the registrations and mark the form accepted, in one transaction."""
        ...

    def reject_request_form(
        self, form_id: int, actor: str, review_note: str,
        records: Sequence[AuditRecord],
    ) -> Dict[str, Any]:
        ...

    # --- audit -----------------------------------------------------------
    def write_audit(self, records: Sequence[AuditRecord]) -> None:
        ...

    def audit_for(self, entity: str, entity_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        ...
