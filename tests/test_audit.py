"""Tests for the audit layer.

With no approval step, these records are the only account of why anything
changed, so the cases that matter are the ones that would quietly produce a
wrong or missing record.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app import audit


# --------------------------------------------------------------------------
# value normalisation
# --------------------------------------------------------------------------

def test_none_and_blank_are_the_same_absence():
    assert audit.normalise(None) is None
    assert audit.normalise("") is None
    assert audit.normalise("   ") is None


def test_a_date_and_its_iso_string_compare_equal():
    """The commonest false positive: a date from the form vs one from the DB."""
    assert audit.normalise(dt.date(2026, 1, 31)) == audit.normalise("2026-01-31")


def test_booleans_render_consistently():
    assert audit.normalise(True) == "true"
    assert audit.normalise(False) == "false"


def test_whole_floats_do_not_grow_a_decimal_point():
    assert audit.normalise(12.0) == "12"
    assert audit.normalise(Decimal("12.00")) == "12"


def test_surrounding_whitespace_is_not_a_change():
    before = {"comments": "needs review"}
    after = {"comments": "  needs review  "}
    assert audit.diff(before, after) == []


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def test_reports_only_changed_fields():
    before = {"sku": "30005", "expiry_date": dt.date(2026, 1, 1), "comments": "x"}
    after = {"sku": "30005", "expiry_date": dt.date(2027, 1, 1), "comments": "x"}
    changes = audit.diff(before, after)
    assert [c.field for c in changes] == ["expiry_date"]
    assert changes[0].old_value == "2026-01-01"
    assert changes[0].new_value == "2027-01-01"


def test_clearing_a_field_is_a_change():
    changes = audit.diff({"comments": "something"}, {"comments": ""})
    assert len(changes) == 1
    assert changes[0].old_value == "something"
    assert changes[0].new_value is None


def test_setting_a_previously_empty_field_is_a_change():
    changes = audit.diff({"ifu_version": None}, {"ifu_version": "Rev C"})
    assert changes[0].old_value is None
    assert changes[0].new_value == "Rev C"


def test_field_whitelist_ignores_unexpected_keys():
    """A stray key in a payload must not become an audit row."""
    before = {"sku": "1"}
    after = {"sku": "2", "created_by": "attacker@example.com"}
    changes = audit.diff(before, after, fields=["sku"])
    assert [c.field for c in changes] == ["sku"]


def test_absent_keys_are_not_treated_as_cleared():
    """A PATCH sends only what it changes; omitted fields are not deletions."""
    before = {"sku": "1", "comments": "keep me"}
    after = {"sku": "2"}
    changes = audit.diff(before, after, fields=["sku", "comments"])
    assert [c.field for c in changes] == ["sku"]


# --------------------------------------------------------------------------
# reasons
# --------------------------------------------------------------------------

def test_a_change_without_a_reason_is_rejected():
    with pytest.raises(audit.ReasonRequired):
        audit.check_reason(None)
    with pytest.raises(audit.ReasonRequired):
        audit.check_reason("   ")
    with pytest.raises(audit.ReasonRequired):
        audit.check_reason("x")


def test_a_real_reason_is_accepted_and_trimmed():
    assert audit.check_reason("  renewal received  ") == "renewal received"


def test_records_cannot_be_built_without_a_reason():
    changes = [audit.FieldChange("sku", "1", "2")]
    with pytest.raises(audit.ReasonRequired):
        audit.records_for_update("a@b.com", "registration", 1, changes, reason="")


# --------------------------------------------------------------------------
# record construction
# --------------------------------------------------------------------------

def test_one_record_per_changed_field():
    changes = [
        audit.FieldChange("sku", "1", "2"),
        audit.FieldChange("expiry_date", "2026-01-01", "2027-01-01"),
    ]
    records = audit.records_for_update(
        "ra@example.com", "registration", 42, changes, reason="renewal granted"
    )
    assert len(records) == 2
    assert {r.field for r in records} == {"sku", "expiry_date"}
    assert all(r.entity_id == "42" for r in records)
    assert all(r.actor == "ra@example.com" for r in records)
    assert all(r.action == "update" for r in records)


def test_one_user_action_shares_a_request_id():
    changes = [audit.FieldChange("a", None, "1"), audit.FieldChange("b", None, "2")]
    records = audit.records_for_update("x@y.z", "registration", 1, changes, reason="load")
    assert len({r.request_id for r in records}) == 1


def test_separate_actions_get_separate_request_ids():
    change = [audit.FieldChange("a", None, "1")]
    first = audit.records_for_update("x@y.z", "registration", 1, change, reason="one")
    second = audit.records_for_update("x@y.z", "registration", 1, change, reason="two")
    assert first[0].request_id != second[0].request_id


def test_insert_and_delete_carry_no_field():
    created = audit.record_for_insert("x@y.z", "registration", 7, reason="new licence")
    removed = audit.record_for_delete("x@y.z", "registration", 7, reason="duplicate")
    assert created.action == "insert" and created.field is None
    assert removed.action == "delete" and removed.field is None


def test_timestamps_are_timezone_aware():
    """Naive timestamps in an audit trail are not defensible."""
    record = audit.record_for_insert("x@y.z", "registration", 1, reason="new")
    assert record.occurred_at.tzinfo is not None


def test_no_changes_produces_no_records():
    records = audit.records_for_update("x@y.z", "registration", 1, [], reason="no-op")
    assert records == []
