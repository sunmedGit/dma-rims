"""In-memory store for local development.

Lets the whole application run on a laptop with no database, so a form can be
uploaded and the flow walked through before Lakebase exists.

Everything is lost when the process stops. It is only ever selected when
DMA_DEV_MODE is on AND no database is configured, it says so on /api/health,
and it logs a warning at startup. It must never be what a deployed app falls
back to silently.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .audit import AuditRecord
from .intake.form_parser import LicenceGroup  # noqa: F401  (documents the shape)

log = logging.getLogger(__name__)

# Enough markets to exercise the flow. The real list is loaded from the
# country table in a deployed app.
_COUNTRIES = [
    ("458", "MY", "Malaysia"),
    ("840", "US", "United States"),
    ("124", "CA", "Canada"),
    ("276", "DE", "Germany"),
    ("250", "FR", "France"),
    ("826", "GB", "United Kingdom"),
    ("036", "AU", "Australia"),
    ("392", "JP", "Japan"),
    ("076", "BR", "Brazil"),
    ("484", "MX", "Mexico"),
    ("702", "SG", "Singapore"),
    ("360", "ID", "Indonesia"),
    ("764", "TH", "Thailand"),
    ("608", "PH", "Philippines"),
    ("704", "VN", "Vietnam"),
    ("356", "IN", "India"),
]


class MemoryStore:
    def __init__(self):
        self.registrations: Dict[int, Dict[str, Any]] = {}
        self.holds: Dict[int, Dict[str, Any]] = {}
        self.forms: Dict[int, Dict[str, Any]] = {}
        self.audit_rows: List[AuditRecord] = []
        self.countries = {
            name.lower(): {"code": code, "alpha2": alpha2, "name": name}
            for code, alpha2, name in _COUNTRIES
        }
        self._next = {"registration": 1, "hold": 1, "form": 1}

    def _take(self, key: str) -> int:
        value = self._next[key]
        self._next[key] = value + 1
        return value

    # -- registrations ----------------------------------------------------

    def list_registrations(
        self, country_code=None, product_class_code=None, status=None,
        search=None, include_archived=False, limit=250,
    ) -> List[Dict[str, Any]]:
        rows = list(self.registrations.values())
        if not include_archived:
            rows = [r for r in rows if not r.get("archived")]
        if country_code:
            rows = [r for r in rows if r.get("country_code") == country_code]
        if product_class_code:
            rows = [r for r in rows if r.get("product_class_code") == product_class_code]
        if search:
            needle = search.lower()
            rows = [
                r for r in rows
                if needle in " ".join(
                    str(r.get(f) or "") for f in
                    ("registration_number", "sku", "product_description")
                ).lower()
            ]
        return rows[:limit]

    def get_registration(self, registration_id: int) -> Optional[Dict[str, Any]]:
        return self.registrations.get(registration_id)

    def update_registration(self, registration_id, changes, actor, records):
        row = self.registrations.get(registration_id)
        if row is None:
            raise LookupError("No registration with id %s" % registration_id)
        row.update(changes)
        row["updated_by"] = actor
        self.write_audit(records)
        return row

    # -- gtins and holds --------------------------------------------------

    def list_gtins(self, gtin=None, country_code=None) -> List[Dict[str, Any]]:
        return []

    def apply_hold(self, gtin, scope, reason, source_ref, actor, records):
        hold_id = self._take("hold")
        hold = {
            "id": hold_id, "gtin": gtin, "scope": scope, "reason": reason,
            "source_ref": source_ref, "applied_by": actor,
            "lifted_at": None, "lifted_by": None, "lift_reason": None,
        }
        self.holds[hold_id] = hold
        self.write_audit(records)
        return hold

    def lift_hold(self, hold_id, actor, lift_reason, records):
        hold = self.holds.get(hold_id)
        if hold is None or hold["lifted_at"] is not None:
            raise LookupError(
                "Hold %s does not exist or has already been lifted." % hold_id
            )
        hold.update({"lifted_at": "now", "lifted_by": actor, "lift_reason": lift_reason})
        self.write_audit(records)
        return hold

    # -- request forms ----------------------------------------------------

    def find_country_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self.countries.get((name or "").strip().lower())

    def create_request_form(self, fields, actor, records):
        form_id = self._take("form")
        row = dict(fields)
        row.update({"id": form_id, "uploaded_by": actor, "state": "parsed"})
        self.forms[form_id] = row
        self.write_audit(records)
        return row

    def get_request_form(self, form_id: int) -> Optional[Dict[str, Any]]:
        return self.forms.get(form_id)

    def list_request_forms(self, state=None, limit=100) -> List[Dict[str, Any]]:
        rows = list(self.forms.values())
        if state:
            rows = [r for r in rows if r.get("state") == state]
        return rows[:limit]

    def accept_request_form(self, form_id, registrations, actor, review_note, records):
        form = self.forms.get(form_id)
        if form is None:
            raise LookupError("No request form with id %s" % form_id)
        if form["state"] != "parsed":
            raise ValueError("This form has already been %s." % form["state"])

        created = []
        for payload in registrations:
            reg_id = self._take("registration")
            row = {k: v for k, v in payload.items() if not k.startswith("_")}
            row.update(
                {
                    "id": reg_id,
                    "registration_number": None,
                    "source_form_id": form_id,
                    "created_by": actor,
                    "updated_by": actor,
                    "archived": False,
                    "in_progress": False,
                    "status": "Pending",
                    "licence_group": payload.get("_licence_group", 0),
                }
            )
            self.registrations[reg_id] = row
            created.append(row)

        form.update(
            {"state": "accepted", "reviewed_by": actor, "review_note": review_note}
        )
        self.write_audit(records)
        return created

    def reject_request_form(self, form_id, actor, review_note, records):
        form = self.forms.get(form_id)
        if form is None or form["state"] != "parsed":
            raise ValueError(
                "Form %s does not exist or has already been reviewed." % form_id
            )
        form.update(
            {"state": "rejected", "reviewed_by": actor, "review_note": review_note}
        )
        self.write_audit(records)
        return form

    # -- audit ------------------------------------------------------------

    def write_audit(self, records: Sequence[AuditRecord]) -> None:
        self.audit_rows.extend(records)

    def audit_for(self, entity, entity_id, limit=200) -> List[Dict[str, Any]]:
        return [
            {
                "occurred_at": r.occurred_at.isoformat(),
                "actor": r.actor,
                "action": r.action,
                "field": r.field,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "reason": r.reason,
                "request_id": r.request_id,
            }
            for r in reversed(self.audit_rows)
            if r.entity == entity and r.entity_id == str(entity_id)
        ][:limit]

    def close(self) -> None:
        pass
