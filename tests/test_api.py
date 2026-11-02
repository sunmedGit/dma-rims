"""API tests.

A fake store stands in for Lakebase so the route behaviour — authorization,
the mandatory reason, and what reaches the audit trail — is tested on its own.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import main
from app.config import Settings
from app.identity import StaticGroupResolver

EDITOR = "ra.lead@example.com"
VIEWER = "reader@example.com"
GROUP = "ra-editors"


class FakeStore:
    def __init__(self):
        self.registrations = {
            1: {
                "id": 1,
                "registration_number": "REG-458-1001",
                "country_code": "458",
                "sku": "30005",
                "expiry_date": dt.date(2027, 3, 31),
                "comments": "initial load",
                "archived": False,
                "status": "Active",
            }
        }
        self.audit_rows = []
        self.holds = {}
        self._next_hold = 1

    # --- registrations ---
    def list_registrations(self, **kwargs):
        rows = list(self.registrations.values())
        if not kwargs.get("include_archived"):
            rows = [r for r in rows if not r["archived"]]
        if kwargs.get("country_code"):
            rows = [r for r in rows if r["country_code"] == kwargs["country_code"]]
        return rows[: kwargs.get("limit", 250)]

    def get_registration(self, registration_id):
        return self.registrations.get(registration_id)

    def update_registration(self, registration_id, changes, actor, records):
        self.registrations[registration_id].update(changes)
        self.write_audit(records)
        return self.registrations[registration_id]

    # --- gtins / holds ---
    def list_gtins(self, gtin=None, country_code=None):
        return []

    def apply_hold(self, gtin, scope, reason, source_ref, actor, records):
        hold = {
            "id": self._next_hold, "gtin": gtin, "scope": scope,
            "reason": reason, "source_ref": source_ref,
            "applied_by": actor, "lifted_at": None,
        }
        self.holds[self._next_hold] = hold
        self._next_hold += 1
        self.write_audit(records)
        return hold

    def lift_hold(self, hold_id, actor, lift_reason, records):
        hold = self.holds[hold_id]
        hold["lifted_at"] = "now"
        hold["lifted_by"] = actor
        hold["lift_reason"] = lift_reason
        self.write_audit(records)
        return hold

    # --- audit ---
    def write_audit(self, records):
        self.audit_rows.extend(records)

    def audit_for(self, entity, entity_id, limit=200):
        return [
            {
                "field": r.field, "old_value": r.old_value, "new_value": r.new_value,
                "reason": r.reason, "actor": r.actor,
            }
            for r in self.audit_rows
            if r.entity == entity and r.entity_id == entity_id
        ][:limit]


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def client(store, monkeypatch):
    settings = Settings(
        database_url="fake://",
        evidence_volume="/Volumes/x/y/evidence",
        editor_group=GROUP,
        editor_emails=frozenset({EDITOR}),
        static_dir=main._settings.static_dir,
        dev_mode=False,
        dev_user_email="dev@localhost",
    )
    monkeypatch.setattr(main, "_settings", settings)
    monkeypatch.setattr(
        main, "_resolver", StaticGroupResolver(frozenset({EDITOR}), group_name=GROUP)
    )
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    main.app.dependency_overrides[main.get_store] = lambda: store
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def as_editor(**extra):
    return {"X-Forwarded-Email": EDITOR, **extra}


def as_viewer(**extra):
    return {"X-Forwarded-Email": VIEWER, **extra}


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------

def test_health_needs_no_identity(client):
    assert client.get("/api/health").status_code == 200


def test_me_reports_the_access_level(client):
    assert client.get("/api/me", headers=as_editor()).json()["can_edit"] is True
    assert client.get("/api/me", headers=as_viewer()).json()["can_edit"] is False


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def test_a_viewer_can_read_registrations(client):
    response = client.get("/api/registrations", headers=as_viewer())
    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_archived_records_are_hidden_by_default(client, store):
    store.registrations[1]["archived"] = True
    assert client.get("/api/registrations", headers=as_viewer()).json()["count"] == 0
    response = client.get(
        "/api/registrations?include_archived=true", headers=as_viewer()
    )
    assert response.json()["count"] == 1


def test_unknown_registration_is_a_404(client):
    assert client.get("/api/registrations/999", headers=as_viewer()).status_code == 404


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def test_a_viewer_cannot_change_a_registration(client, store):
    response = client.patch(
        "/api/registrations/1",
        headers=as_viewer(),
        json={"reason": "trying it on", "changes": {"comments": "edited"}},
    )
    assert response.status_code == 403
    assert store.registrations[1]["comments"] == "initial load"
    assert store.audit_rows == []


def test_an_editor_can_change_a_registration(client, store):
    response = client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "renewal certificate received", "changes": {"comments": "renewed"}},
    )
    assert response.status_code == 200
    assert store.registrations[1]["comments"] == "renewed"
    assert response.json()["changed"] == ["comments"]


def test_a_change_without_a_reason_is_refused(client, store):
    response = client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"changes": {"comments": "no reason given"}},
    )
    assert response.status_code == 422
    assert store.registrations[1]["comments"] == "initial load"


def test_a_blank_reason_is_refused(client, store):
    response = client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "  ", "changes": {"comments": "x"}},
    )
    assert response.status_code == 422
    assert store.audit_rows == []


def test_every_changed_field_produces_an_audit_row_with_the_reason(client, store):
    client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={
            "reason": "renewal granted by NPRA",
            "changes": {"comments": "renewed", "expiry_date": "2029-03-31"},
        },
    )
    assert len(store.audit_rows) == 2
    assert {r.field for r in store.audit_rows} == {"comments", "expiry_date"}
    assert all(r.reason == "renewal granted by NPRA" for r in store.audit_rows)
    assert all(r.actor == EDITOR for r in store.audit_rows)
    assert len({r.request_id for r in store.audit_rows}) == 1


def test_audit_rows_record_the_previous_value(client, store):
    client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "correcting a typo", "changes": {"comments": "corrected"}},
    )
    row = store.audit_rows[0]
    assert row.old_value == "initial load"
    assert row.new_value == "corrected"


def test_resending_identical_values_writes_nothing(client, store):
    """A save button that re-posts the form must not fill the trail with noise."""
    response = client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "no change intended", "changes": {"comments": "initial load"}},
    )
    assert response.status_code == 200
    assert response.json()["changed"] == []
    assert store.audit_rows == []


def test_a_date_sent_as_a_string_is_not_a_spurious_change(client, store):
    client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "resaving the record", "changes": {"expiry_date": "2027-03-31"}},
    )
    assert store.audit_rows == []


def test_fields_outside_the_whitelist_are_ignored(client, store):
    client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={
            "reason": "attempting to set an internal column",
            "changes": {"id": 99, "created_by": "someone@else.com", "comments": "ok"},
        },
    )
    assert store.registrations[1]["id"] == 1
    assert "created_by" not in store.registrations[1]
    assert [r.field for r in store.audit_rows] == ["comments"]


def test_history_reads_back_the_change(client):
    client.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "renewal received", "changes": {"comments": "renewed"}},
    )
    history = client.get(
        "/api/registrations/1/history", headers=as_viewer()
    ).json()["history"]
    assert history[0]["reason"] == "renewal received"
    assert history[0]["new_value"] == "renewed"


# --------------------------------------------------------------------------
# holds
# --------------------------------------------------------------------------

def test_a_viewer_cannot_apply_a_hold(client, store):
    response = client.post(
        "/api/holds",
        headers=as_viewer(),
        json={"gtin": "00812457000934", "reason": "supplier CAPA open"},
    )
    assert response.status_code == 403
    assert store.holds == {}


def test_an_editor_applies_and_lifts_a_hold(client, store):
    created = client.post(
        "/api/holds",
        headers=as_editor(),
        json={"gtin": "00812457000934", "reason": "supplier CAPA open",
              "source_ref": "CAPA-2216"},
    )
    assert created.status_code == 201
    hold_id = created.json()["hold"]["id"]

    lifted = client.post(
        "/api/holds/%d/lift" % hold_id,
        headers=as_editor(),
        json={"reason": "CAPA closed, stock re-inspected"},
    )
    assert lifted.status_code == 200
    assert store.holds[hold_id]["lifted_at"] is not None
    assert len(store.audit_rows) == 2


def test_a_hold_without_a_reason_is_refused(client, store):
    response = client.post(
        "/api/holds", headers=as_editor(), json={"gtin": "00812457000934"}
    )
    assert response.status_code == 422
    assert store.holds == {}


# --------------------------------------------------------------------------
# authorization must not depend on the database being reachable
# --------------------------------------------------------------------------

@pytest.fixture
def client_without_database(client):
    """The store dependency fails, as it does when Lakebase is unreachable."""

    def unavailable():
        raise HTTPException(status_code=503, detail="No database is configured.")

    main.app.dependency_overrides[main.get_store] = unavailable
    yield client


def test_a_viewer_is_refused_even_when_the_database_is_down(client_without_database):
    """403, not 503.

    FastAPI resolves dependencies in parameter order, so declaring the store
    before the user made an unreachable database mask the real answer: that
    this caller may not write at all. A viewer must be told no regardless of
    infrastructure state.
    """
    response = client_without_database.patch(
        "/api/registrations/1",
        headers=as_viewer(),
        json={"reason": "trying it on", "changes": {"comments": "edited"}},
    )
    assert response.status_code == 403


def test_a_viewer_is_refused_a_hold_when_the_database_is_down(client_without_database):
    response = client_without_database.post(
        "/api/holds",
        headers=as_viewer(),
        json={"gtin": "00812457000934", "reason": "supplier CAPA open"},
    )
    assert response.status_code == 403


def test_an_editor_still_gets_503_when_the_database_is_down(client_without_database):
    response = client_without_database.patch(
        "/api/registrations/1",
        headers=as_editor(),
        json={"reason": "renewal received", "changes": {"comments": "renewed"}},
    )
    assert response.status_code == 503
