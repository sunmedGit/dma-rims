"""Tests for the Registration Request Form reader.

Fixtures are built in code rather than committed as .xlsx files: the real forms
carry distributor contact numbers and signatory names, which do not belong in
the repository. The structure below mirrors a real form; the values are invented.
"""

from __future__ import annotations

import datetime as dt
import io

import openpyxl
import pytest

from app.intake.form_parser import FormParseError, parse_registration_request


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------

HEADER_ROW = [
    "Brand",
    "Skus Requested",
    "Product Classification (Requesting Country)",
    "Product Description",
    "List of Requirements",
]

NOTE = "1 license application - we will confirm once all documents received"


def _workbook(
    *,
    header_offset=0,
    sheet_title="Registration Request Form",
    country="Testland",
    distributor="Example Distribution Sdn Bhd",
    ra_contact="000-0000000",
    request_date=45954,
    licences_expected=2,
    sku_blocks=None,
    signature="A. Tester",
    signature_date="24/10/2025",
    requirements=True,
):
    """Build a form workbook in memory and return it as a BytesIO."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_title

    for _ in range(header_offset):
        ws.append([])

    ws.append(["Customer Input (or Rep Input)"])
    ws.append(["Country", country])
    ws.append(["Distributor Name", distributor])
    ws.append(["Customer RA Contact", ra_contact, None, None, "# of Licenses Expected"])
    ws.append(["Date of Request", request_date, None, None, licences_expected])
    ws.append([])
    ws.append(HEADER_ROW)

    if sku_blocks is None:
        sku_blocks = [
            ("Widget Alpha", ["10005", "10055", "10105"]),
            ("Widget Beta", ["20005", "20055"]),
        ]
    for block_idx, (family, skus) in enumerate(sku_blocks):
        for sku_idx, sku in enumerate(skus):
            ws.append(
                [
                    "AirLife",
                    sku,
                    "%s - Class B" % country,
                    "(%s) %s Size %d" % (sku, family, sku_idx),
                    NOTE if sku_idx == 0 and block_idx is not None else None,
                ]
            )

    ws.append([])
    if signature is not None:
        ws.append([None, None, None, None, "Add signature      %s" % signature])
    if signature_date is not None:
        ws.append([None, None, None, None, "Date of Request    %s" % signature_date])

    if requirements:
        rs = wb.create_sheet("List of Requirements")
        rs.append(["List of Requirements"])
        rs.append([])
        rs.append([None, "Requirement", "Additional Comments", "Received"])
        rs.append([1, "CFG", "Wetransfer", "Yes"])
        rs.append([2, "CE", "Wetransfer", "Yes"])
        rs.append([3, "ISO13485", "Wetransfer", None])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --------------------------------------------------------------------------
# header block
# --------------------------------------------------------------------------

def test_reads_the_header_block():
    form = parse_registration_request(_workbook())
    assert form.country == "Testland"
    assert form.distributor == "Example Distribution Sdn Bhd"
    assert form.ra_contact == "000-0000000"
    assert form.licences_expected == 2
    assert form.source_sheet == "Registration Request Form"


def test_converts_an_excel_serial_date():
    form = parse_registration_request(_workbook(request_date=45954))
    assert form.request_date == dt.date(2025, 10, 24)


def test_accepts_a_real_datetime_for_the_request_date():
    form = parse_registration_request(
        _workbook(request_date=dt.datetime(2026, 3, 1, 9, 30))
    )
    assert form.request_date == dt.date(2026, 3, 1)


def test_finds_fields_when_rows_are_inserted_above():
    """People add rows at the top; nothing may depend on fixed addresses."""
    form = parse_registration_request(_workbook(header_offset=4))
    assert form.country == "Testland"
    assert form.licences_expected == 2
    assert form.sku_count == 5


def test_finds_the_form_on_a_renamed_sheet():
    form = parse_registration_request(_workbook(sheet_title="RRF Malaysia 2026"))
    assert form.country == "Testland"
    assert form.source_sheet == "RRF Malaysia 2026"


# --------------------------------------------------------------------------
# SKU table and licence grouping
# --------------------------------------------------------------------------

def test_groups_skus_under_each_licence_note():
    form = parse_registration_request(_workbook())
    assert form.sku_count == 5
    assert [len(g.skus) for g in form.groups] == [3, 2]
    assert form.groups[0].sku_numbers == ["10005", "10055", "10105"]
    assert form.groups[1].sku_numbers == ["20005", "20055"]
    assert form.groups[0].note == NOTE


def test_captures_classification_per_group():
    form = parse_registration_request(_workbook())
    assert form.groups[0].classifications == ["Testland - Class B"]


def test_sku_rows_carry_their_spreadsheet_row_number():
    """Row numbers let the UI point a reviewer at the offending line."""
    form = parse_registration_request(_workbook())
    assert form.skus[0].row == 8
    assert form.skus[0].sku == "10005"


def test_warns_when_a_trailing_sku_breaks_the_group_pattern():
    """The real failure: unrelated SKUs tacked onto the last licence."""
    form = parse_registration_request(
        _workbook(
            licences_expected=2,
            sku_blocks=[
                ("Widget Alpha", ["10005", "10055"]),
                ("Widget Beta", ["20005", "20055"]),
            ],
        )
    )
    # append a stylet-style SKU with no note of its own
    assert len(form.groups) == 2

    wb = openpyxl.load_workbook(_workbook(licences_expected=2))
    ws = wb["Registration Request Form"]
    ws.insert_rows(13)
    ws.cell(row=13, column=1, value="AirLife")
    ws.cell(row=13, column=2, value="99-9999")
    ws.cell(row=13, column=3, value="Testland - Class B")
    ws.cell(row=13, column=4, value="STYLET SOMETHING ELSE ENTIRELY")
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    form = parse_registration_request(buf)
    assert "99-9999" in form.groups[-1].sku_numbers
    assert any("Confirm they belong there" in w for w in form.warnings)


def test_warns_when_skus_precede_the_first_licence_note():
    buf = _workbook()
    wb = openpyxl.load_workbook(buf)
    ws = wb["Registration Request Form"]
    ws.insert_rows(8)
    ws.cell(row=8, column=1, value="AirLife")
    ws.cell(row=8, column=2, value="00001")
    ws.cell(row=8, column=4, value="Orphan line")
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)

    form = parse_registration_request(out)
    assert any("above the first licence note" in w for w in form.warnings)
    assert form.skus[0].licence_group == 0


def test_stops_at_the_signature_block():
    form = parse_registration_request(_workbook())
    assert all(s.sku not in ("", None) for s in form.skus)
    assert form.sku_count == 5


# --------------------------------------------------------------------------
# signature
# --------------------------------------------------------------------------

def test_reads_a_signature_sharing_a_cell_with_its_label():
    form = parse_registration_request(_workbook(signature="N. Example"))
    assert form.signed_by == "N. Example"
    assert form.signature_date == dt.date(2025, 10, 24)


def test_warns_when_the_form_is_unsigned():
    form = parse_registration_request(_workbook(signature=None))
    assert form.signed_by == ""
    assert any("not signed" in w for w in form.warnings)


# --------------------------------------------------------------------------
# requirements sheet
# --------------------------------------------------------------------------

def test_reads_the_requirements_checklist():
    form = parse_registration_request(_workbook())
    assert [r.requirement for r in form.requirements] == ["CFG", "CE", "ISO13485"]
    assert form.requirements[0].comments == "Wetransfer"
    assert form.requirements[0].received == "Yes"


def test_title_row_is_not_mistaken_for_the_requirements_header():
    """'List of Requirements' in A1 must not be read as the header row."""
    form = parse_registration_request(_workbook())
    assert len(form.requirements) == 3


def test_warns_about_outstanding_requirements():
    form = parse_registration_request(_workbook())
    assert any("no 'Received' entry" in w for w in form.warnings)


def test_requirements_sheet_is_optional():
    form = parse_registration_request(_workbook(requirements=False))
    assert form.requirements == []
    assert form.country == "Testland"


# --------------------------------------------------------------------------
# validation and failure
# --------------------------------------------------------------------------

def test_warns_when_licence_count_disagrees_with_the_notes():
    form = parse_registration_request(_workbook(licences_expected=5))
    assert any("expects 5 licence" in w for w in form.warnings)


def test_no_count_warning_when_the_form_agrees():
    form = parse_registration_request(_workbook(licences_expected=2))
    assert not any("licence note(s) were found" in w for w in form.warnings)


def test_warns_about_missing_header_fields():
    form = parse_registration_request(_workbook(country="", distributor=""))
    assert any("Missing from the form header" in w for w in form.warnings)


def test_rejects_a_file_that_is_not_a_workbook():
    with pytest.raises(FormParseError):
        parse_registration_request(io.BytesIO(b"this is not a spreadsheet"))


def test_rejects_a_workbook_with_no_request_table():
    wb = openpyxl.Workbook()
    wb.active["A1"] = "Some other spreadsheet"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    with pytest.raises(FormParseError):
        parse_registration_request(buf)


def test_to_dict_is_json_safe():
    import json

    form = parse_registration_request(_workbook())
    payload = json.dumps(form.to_dict())
    assert "Testland" in payload
