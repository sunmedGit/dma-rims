"""Intake endpoint tests: upload a form, review it, accept or reject.

The split into two steps is the thing being tested. Upload must never create
registrations, because the parser's warnings exist precisely for the cases a
machine should not decide.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app import files, main
from app.config import Settings
from app.identity import StaticGroupResolver
from tests.test_api import EDITOR, GROUP, FakeStore, as_editor, as_viewer
from tests.test_form_parser import _workbook


class IntakeFakeStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.forms = {}
        self._next_form = 1
        self._next_reg = 100
        self.countries = {"testland": {"code": "999", "alpha2": "TL", "name": "Testland"}}

    def find_country_by_name(self, name):
        return self.countries.get((name or "").strip().lower())

    def create_request_form(self, fields, actor, records):
        form_id = self._next_form
        self._next_form += 1
        row = dict(fields)
        row.update({"id": form_id, "uploaded_by": actor, "state": "parsed"})
        self.forms[form_id] = row
        self.write_audit(records)
        return row

    def get_request_form(self, form_id):
        return self.forms.get(form_id)

    def list_request_forms(self, state=None, limit=100):
        rows = list(self.forms.values())
        if state:
            rows = [r for r in rows if r["state"] == state]
        return rows[:limit]

    def accept_request_form(self, form_id, registrations, actor, review_note, records):
        form = self.forms[form_id]
        if form["state"] != "parsed":
            raise ValueError("This form has already been %s." % form["state"])
        created = []
        for payload in registrations:
            reg_id = self._next_reg
            self._next_reg += 1
            row = {k: v for k, v in payload.items() if not k.startswith("_")}
            row["id"] = reg_id
            self.registrations[reg_id] = row
            created.append(row)
        form["state"] = "accepted"
        form["review_note"] = review_note
        self.write_audit(records)
        return created

    def reject_request_form(self, form_id, actor, review_note, records):
        form = self.forms.get(form_id)
        if form is None or form["state"] != "parsed":
            raise ValueError("Form does not exist or has already been reviewed.")
        form["state"] = "rejected"
        form["review_note"] = review_note
        self.write_audit(records)
        return form


@pytest.fixture
def store():
    return IntakeFakeStore()


@pytest.fixture
def client(store, tmp_path, monkeypatch):
    settings = Settings(
        database_url="fake://",
        evidence_volume=str(tmp_path / "vol"),
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
    monkeypatch.setattr(main, "_file_store", files.LocalFileStore(tmp_path / "vol"))
    main.app.dependency_overrides[main.get_settings] = lambda: settings
    main.app.dependency_overrides[main.get_store] = lambda: store
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def upload(client, headers=None, **kwargs):
    buf = _workbook(**kwargs)
    return client.post(
        "/api/intake/forms",
        headers=headers or as_editor(),
        files={
            "file": (
                "request.xlsx",
                buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


# --------------------------------------------------------------------------
# upload
# --------------------------------------------------------------------------

def test_a_viewer_cannot_upload_a_form(client, store):
    assert upload(client, headers=as_viewer()).status_code == 403
    assert store.forms == {}


def test_upload_parses_the_workbook(client):
    body = upload(client).json()
    assert body["form"]["country_name"] == "Testland"
    assert body["form"]["distributor"] == "Example Distribution Sdn Bhd"
    assert body["form"]["sku_count"] == 5
    assert len(body["parsed"]["groups"]) == 2


def test_upload_resolves_the_country_to_a_code(client):
    assert upload(client).json()["form"]["country_code"] == "999"


def test_upload_creates_no_registrations(client, store):
    """The whole point of the two-step flow."""
    upload(client)
    assert store.registrations == {1: store.registrations[1]}  # only the seeded one


def test_upload_stores_the_file_and_records_its_path(client, tmp_path):
    path = upload(client).json()["form"]["volume_path"]
    assert "request-forms" in path
    assert (tmp_path / "vol" / path.lstrip("/")).exists()


def test_a_file_that_is_not_a_workbook_is_refused(client, store):
    response = client.post(
        "/api/intake/forms",
        headers=as_editor(),
        files={"file": ("notes.txt", b"just some text", "text/plain")},
    )
    assert response.status_code == 422
    assert store.forms == {}


def test_an_unknown_country_is_flagged_rather_than_guessed(client):
    body = upload(client, country="Atlantis").json()
    assert body["form"]["country_code"] is None
    assert any("does not match any country on file" in w for w in body["warnings"])


def test_parser_warnings_reach_the_response(client):
    body = upload(client, signature=None).json()
    assert any("not signed" in w for w in body["warnings"])


def test_upload_is_audited(client, store):
    upload(client)
    assert any(r.entity == "request_form" for r in store.audit_rows)


# --------------------------------------------------------------------------
# accept
# --------------------------------------------------------------------------

def test_accept_creates_one_registration_per_licence_group(client, store):
    form_id = upload(client).json()["form"]["id"]
    response = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(),
        json={"reason": "grouping confirmed with RA"},
    )
    assert response.status_code == 200
    assert response.json()["created"] == 2

    created = [r for r in store.registrations.values() if r.get("pending_type")]
    assert len(created) == 2
    assert created[0]["country_code"] == "999"
    assert created[0]["pending_type"] == "Initial Registration"
    assert "10005" in created[0]["sku"]


def test_accept_carries_the_distributor_through(client, store):
    form_id = upload(client).json()["form"]["id"]
    client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(),
        json={"reason": "confirmed"},
    )
    created = [r for r in store.registrations.values() if r.get("pending_type")]
    assert created[0]["local_distributors"] == "Example Distribution Sdn Bhd"


def test_a_viewer_cannot_accept(client, store):
    form_id = upload(client).json()["form"]["id"]
    response = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_viewer(),
        json={"reason": "trying it on"},
    )
    assert response.status_code == 403
    assert store.forms[form_id]["state"] == "parsed"


def test_accept_without_a_reason_is_refused(client, store):
    form_id = upload(client).json()["form"]["id"]
    response = client.post(
        "/api/intake/forms/%d/accept" % form_id, headers=as_editor(), json={}
    )
    assert response.status_code == 422
    assert store.forms[form_id]["state"] == "parsed"


def test_a_form_cannot_be_accepted_twice(client, store):
    """A double click must not duplicate every registration on the form."""
    form_id = upload(client).json()["form"]["id"]
    first = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(), json={"reason": "confirmed"},
    )
    second = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(), json={"reason": "confirmed again"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert len([r for r in store.registrations.values() if r.get("pending_type")]) == 2


def test_a_form_with_an_unresolved_country_cannot_be_accepted(client):
    form_id = upload(client, country="Atlantis").json()["form"]["id"]
    response = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(), json={"reason": "go ahead anyway"},
    )
    assert response.status_code == 422
    assert "not on file" in response.json()["detail"]


def test_the_reviewer_can_correct_the_grouping(client, store):
    """The answer to the parser's grouping warning: let a human fix it."""
    form_id = upload(client).json()["form"]["id"]
    response = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(),
        json={
            "reason": "split the stylets into their own application",
            "groups": [
                {"note": "family A", "skus": ["10005", "10055"]},
                {"note": "family B", "skus": ["10105"]},
                {"note": "stylets", "skus": ["20005", "20055"]},
            ],
        },
    )
    assert response.json()["created"] == 3
    created = [r for r in store.registrations.values() if r.get("pending_type")]
    assert created[2]["comments"] == "stylets"


def test_accepting_is_audited_with_the_reason(client, store):
    form_id = upload(client).json()["form"]["id"]
    store.audit_rows.clear()
    client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(), json={"reason": "grouping confirmed with RA"},
    )
    assert any(r.reason == "grouping confirmed with RA" for r in store.audit_rows)


# --------------------------------------------------------------------------
# reject and listing
# --------------------------------------------------------------------------

def test_a_form_can_be_rejected(client, store):
    form_id = upload(client).json()["form"]["id"]
    response = client.post(
        "/api/intake/forms/%d/reject" % form_id,
        headers=as_editor(), json={"reason": "wrong template, asked for a resubmit"},
    )
    assert response.status_code == 200
    assert store.forms[form_id]["state"] == "rejected"


def test_a_rejected_form_cannot_then_be_accepted(client, store):
    form_id = upload(client).json()["form"]["id"]
    client.post(
        "/api/intake/forms/%d/reject" % form_id,
        headers=as_editor(), json={"reason": "wrong template"},
    )
    response = client.post(
        "/api/intake/forms/%d/accept" % form_id,
        headers=as_editor(), json={"reason": "changed my mind"},
    )
    assert response.status_code == 409


def test_forms_can_be_listed_and_filtered_by_state(client):
    upload(client)
    upload(client)
    assert client.get("/api/intake/forms", headers=as_viewer()).json()["count"] == 2
    assert (
        client.get("/api/intake/forms?state=accepted", headers=as_viewer()).json()["count"]
        == 0
    )


def test_a_viewer_can_read_a_form(client):
    form_id = upload(client).json()["form"]["id"]
    assert client.get(
        "/api/intake/forms/%d" % form_id, headers=as_viewer()
    ).status_code == 200


def test_unknown_form_is_a_404(client):
    assert client.get("/api/intake/forms/999", headers=as_viewer()).status_code == 404
