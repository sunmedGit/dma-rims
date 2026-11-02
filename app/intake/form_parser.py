"""Reader for the AirLife Registration Request Form (.xlsx).

The form is filled in by the distributor or the rep, so the layout drifts: rows
get inserted, labels get retyped, the sheet gets renamed. Nothing here depends
on fixed cell addresses — every field is found by its label, and anything that
cannot be found becomes a warning rather than a silent default.

The parser never guesses at a licence grouping. Where the grouping is ambiguous
it records a warning for a human to resolve, because a wrong grouping means a
wrong number of licence applications.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import openpyxl

# Column headers on the SKU table, matched case-insensitively on a substring.
_COL_PATTERNS = {
    "brand": "brand",
    "sku": "skus requested",
    "classification": "product classification",
    "description": "product description",
    "requirements_note": "list of requirements",
}

# Header fields in the top block: label in one column, value in the next.
_HEADER_PATTERNS = {
    "country": "country",
    "distributor": "distributor name",
    "ra_contact": "customer ra contact",
    "request_date": "date of request",
}

_LICENCES_LABEL = "# of licenses expected"
_SIGNATURE_LABEL = "add signature"
_MAX_HEADER_SCAN_ROWS = 15


@dataclass
class SkuLine:
    row: int
    brand: str = ""
    sku: str = ""
    classification: str = ""
    description: str = ""
    requirements_note: str = ""
    licence_group: int = 0


@dataclass
class LicenceGroup:
    """One expected licence application: the note plus the SKUs under it."""

    index: int
    note: str
    skus: List[SkuLine] = field(default_factory=list)

    @property
    def sku_numbers(self) -> List[str]:
        return [s.sku for s in self.skus]

    @property
    def classifications(self) -> List[str]:
        seen: List[str] = []
        for s in self.skus:
            if s.classification and s.classification not in seen:
                seen.append(s.classification)
        return seen


@dataclass
class Requirement:
    number: str = ""
    requirement: str = ""
    comments: str = ""
    received: str = ""


@dataclass
class RequestForm:
    country: str = ""
    distributor: str = ""
    ra_contact: str = ""
    request_date: Optional[_dt.date] = None
    licences_expected: Optional[int] = None
    signed_by: str = ""
    signature_date: Optional[_dt.date] = None
    skus: List[SkuLine] = field(default_factory=list)
    groups: List[LicenceGroup] = field(default_factory=list)
    requirements: List[Requirement] = field(default_factory=list)
    source_sheet: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def sku_count(self) -> int:
        return len(self.skus)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "country": self.country,
            "distributor": self.distributor,
            "ra_contact": self.ra_contact,
            "request_date": self.request_date.isoformat() if self.request_date else None,
            "licences_expected": self.licences_expected,
            "signed_by": self.signed_by,
            "signature_date": self.signature_date.isoformat() if self.signature_date else None,
            "sku_count": self.sku_count,
            "source_sheet": self.source_sheet,
            "groups": [
                {
                    "index": g.index,
                    "note": g.note,
                    "skus": g.sku_numbers,
                    "classifications": g.classifications,
                    # the first line's description stands for the family
                    "description": g.skus[0].description if g.skus else "",
                }
                for g in self.groups
            ],
            "requirements": [
                {
                    "number": r.number,
                    "requirement": r.requirement,
                    "comments": r.comments,
                    "received": r.received,
                }
                for r in self.requirements
            ],
            "warnings": list(self.warnings),
        }


class FormParseError(Exception):
    """The file is not a Registration Request Form we can read."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _norm(value: Any) -> str:
    """Lowercase, collapse whitespace — for matching labels people retype."""
    return re.sub(r"\s+", " ", _text(value)).lower()


def _as_date(value: Any) -> Optional[_dt.date]:
    """Dates arrive as datetimes, Excel serials, or free text."""
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, (int, float)) and 20000 < float(value) < 80000:
        epoch = _dt.date(1899, 12, 30)  # Excel's 1900 leap-year bug
        return epoch + _dt.timedelta(days=int(round(float(value))))
    text = _text(value)
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return _dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _split_label_value(cell_text: str, label: str) -> str:
    """Some cells hold label and value together: 'Add signature    Norliza'."""
    rest = cell_text[len(label):] if _norm(cell_text).startswith(_norm(label)) else cell_text
    return rest.strip(" \t:- ")


def _sheet_rows(sheet) -> List[Sequence[Any]]:
    return [row for row in sheet.iter_rows(values_only=True)]


def _find_form_sheet(workbook) -> Any:
    """The SKU table is the thing that identifies the sheet, not its name."""
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows(values_only=True):
            for cell in row:
                if _COL_PATTERNS["sku"] in _norm(cell):
                    return sheet
    for sheet in workbook.worksheets:
        if "registration request" in _norm(sheet.title):
            return sheet
    raise FormParseError(
        "No Registration Request Form sheet found - expected a column headed "
        "'Skus Requested'."
    )


def _find_requirements_sheet(workbook, exclude) -> Optional[Any]:
    for sheet in workbook.worksheets:
        if sheet is exclude:
            continue
        for row in sheet.iter_rows(values_only=True):
            for cell in row:
                # exact match, so the title 'List of Requirements' does not win
                if _norm(cell) == "requirement":
                    return sheet
    return None


def _parse_header(rows: Sequence[Sequence[Any]], form: RequestForm) -> None:
    for r_idx, row in enumerate(rows[:_MAX_HEADER_SCAN_ROWS]):
        for c_idx, cell in enumerate(row):
            label = _norm(cell)
            if not label:
                continue

            for key, pattern in _HEADER_PATTERNS.items():
                if label == pattern and getattr(form, key) in ("", None):
                    value = row[c_idx + 1] if c_idx + 1 < len(row) else None
                    if key == "request_date":
                        form.request_date = _as_date(value)
                    else:
                        setattr(form, key, _text(value))

            if label == _LICENCES_LABEL and form.licences_expected is None:
                # the count sits beside the label, or directly below it
                candidates = []
                if c_idx + 1 < len(row):
                    candidates.append(row[c_idx + 1])
                if r_idx + 1 < len(rows) and c_idx < len(rows[r_idx + 1]):
                    candidates.append(rows[r_idx + 1][c_idx])
                for candidate in candidates:
                    text = _text(candidate)
                    if re.fullmatch(r"\d+", text):
                        form.licences_expected = int(text)
                        break


def _parse_signature(
    rows: Sequence[Sequence[Any]], form: RequestForm, after_row: int
) -> None:
    """Signature and its date sit at the foot, often label and value in one cell.

    `after_row` is the SKU table header index. Anchoring to the table is what
    keeps the footer's "Date of Request" apart from the header's, whatever the
    length of the SKU list.
    """
    for r_idx, row in enumerate(rows):
        if r_idx <= after_row:
            continue
        for c_idx, cell in enumerate(row):
            text = _text(cell)
            label = _norm(text)
            if label.startswith(_SIGNATURE_LABEL):
                name = _split_label_value(text, _SIGNATURE_LABEL)
                if not name and c_idx + 1 < len(row):
                    name = _text(row[c_idx + 1])
                form.signed_by = name
            elif label.startswith("date of request"):
                rest = _split_label_value(text, "date of request")
                form.signature_date = _as_date(rest) or _as_date(
                    row[c_idx + 1] if c_idx + 1 < len(row) else None
                )


def _find_table_header(rows: Sequence[Sequence[Any]]) -> tuple:
    for r_idx, row in enumerate(rows):
        columns: Dict[str, int] = {}
        for c_idx, cell in enumerate(row):
            label = _norm(cell)
            for key, pattern in _COL_PATTERNS.items():
                if pattern in label and key not in columns:
                    columns[key] = c_idx
        if "sku" in columns:
            return r_idx, columns
    raise FormParseError("Could not find the SKU table header row.")


def _parse_skus(rows: Sequence[Sequence[Any]], form: RequestForm) -> int:
    """Read the SKU table. Returns the header row index, which anchors the footer."""
    header_idx, columns = _find_table_header(rows)
    note_col = columns.get("requirements_note")

    def cell_at(row: Sequence[Any], key: str) -> str:
        idx = columns.get(key)
        if idx is None or idx >= len(row):
            return ""
        return _text(row[idx])

    blank_run = 0
    for r_idx in range(header_idx + 1, len(rows)):
        row = rows[r_idx]
        sku = cell_at(row, "sku")
        note = cell_at(row, "requirements_note") if note_col is not None else ""

        if not sku:
            # the signature block lives below the table and ends it
            if note and _norm(note).startswith((_SIGNATURE_LABEL, "date of request")):
                break
            blank_run += 1
            if blank_run >= 5:
                break
            continue
        blank_run = 0

        line = SkuLine(
            row=r_idx + 1,
            brand=cell_at(row, "brand"),
            sku=sku,
            classification=cell_at(row, "classification"),
            description=cell_at(row, "description"),
            requirements_note=note,
        )
        form.skus.append(line)

    _group_skus(form)
    return header_idx


def _group_skus(form: RequestForm) -> None:
    """A non-empty note opens a new licence group; following SKUs join it."""
    current: Optional[LicenceGroup] = None
    ungrouped: List[SkuLine] = []

    for line in form.skus:
        if line.requirements_note:
            current = LicenceGroup(index=len(form.groups) + 1, note=line.requirements_note)
            form.groups.append(current)
        if current is None:
            ungrouped.append(line)
            continue
        line.licence_group = current.index
        current.skus.append(line)

    if ungrouped:
        form.warnings.append(
            "{n} SKU(s) appear above the first licence note ({skus}) and are not "
            "assigned to a licence application.".format(
                n=len(ungrouped),
                skus=", ".join(s.sku for s in ungrouped[:6]),
            )
        )

    # SKUs trailing the last note may belong to it, or may be a forgotten group.
    if form.groups:
        last = form.groups[-1]
        trailing = [s for s in last.skus if not s.requirements_note]
        if len(trailing) and last.skus and trailing[-1] is last.skus[-1]:
            tail = _trailing_run(last)
            if tail:
                form.warnings.append(
                    "SKU(s) {skus} sit below the last licence note with no note of "
                    "their own and have been grouped into licence {idx}. Confirm "
                    "they belong there.".format(
                        skus=", ".join(s.sku for s in tail), idx=last.index
                    )
                )


def _trailing_run(group: LicenceGroup) -> List[SkuLine]:
    """SKUs at the end of a group whose description breaks the group's pattern."""
    if len(group.skus) < 2:
        return []
    head = _description_stem(group.skus[0].description)
    if not head:
        return []
    tail: List[SkuLine] = []
    for line in reversed(group.skus[1:]):
        if _description_stem(line.description) == head:
            break
        tail.append(line)
    tail.reverse()
    return tail


def _description_stem(description: str) -> str:
    """First two meaningful words, ignoring a leading '(sku)' prefix."""
    cleaned = re.sub(r"^\(\s*[\w-]+\s*\)\s*", "", _text(description))
    words = re.findall(r"[A-Za-z0-9-]+", cleaned)
    return " ".join(w.lower() for w in words[:2])


def _parse_requirements(sheet, form: RequestForm) -> None:
    rows = _sheet_rows(sheet)
    header_idx = -1
    columns: Dict[str, int] = {}
    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row):
            label = _norm(cell)
            if label == "requirement":
                header_idx = r_idx
                columns["requirement"] = c_idx
            elif header_idx == r_idx and "comment" in label:
                columns["comments"] = c_idx
            elif header_idx == r_idx and "received" in label:
                columns["received"] = c_idx
        if header_idx == r_idx:
            columns["number"] = max(columns["requirement"] - 1, 0)
            break
    if header_idx < 0:
        return

    for row in rows[header_idx + 1:]:
        def at(key: str) -> str:
            idx = columns.get(key)
            if idx is None or idx >= len(row):
                return ""
            return _text(row[idx])

        text = at("requirement")
        if not text:
            continue
        form.requirements.append(
            Requirement(
                number=at("number"),
                requirement=text,
                comments=at("comments"),
                received=at("received"),
            )
        )


def _check(form: RequestForm) -> None:
    missing = [
        label
        for label, value in (
            ("Country", form.country),
            ("Distributor Name", form.distributor),
            ("Customer RA Contact", form.ra_contact),
        )
        if not value
    ]
    if missing:
        form.warnings.append("Missing from the form header: " + ", ".join(missing) + ".")
    if form.request_date is None:
        form.warnings.append("No Date of Request could be read from the form header.")
    if not form.signed_by:
        form.warnings.append("The form is not signed.")
    if not form.skus:
        form.warnings.append("No SKUs were found in the request table.")

    expected = form.licences_expected
    if expected is None:
        form.warnings.append("'# of Licenses Expected' is blank on the form.")
    elif form.groups and expected != len(form.groups):
        form.warnings.append(
            "The form expects {expected} licence(s) but {found} licence note(s) were "
            "found in the SKU table.".format(expected=expected, found=len(form.groups))
        )

    outstanding = [r.requirement for r in form.requirements if not r.received]
    if outstanding:
        form.warnings.append(
            "{n} of {total} requirement(s) have no 'Received' entry.".format(
                n=len(outstanding), total=len(form.requirements)
            )
        )


def parse_registration_request(source) -> RequestForm:
    """Read a Registration Request Form.

    `source` is a path, or any file-like object openpyxl accepts.
    Raises FormParseError when the file is not a readable form.
    """
    try:
        workbook = openpyxl.load_workbook(source, data_only=True, read_only=False)
    except FormParseError:
        raise
    except Exception as exc:  # openpyxl raises a wide range on bad input
        raise FormParseError("That file could not be opened as an .xlsx workbook.") from exc

    try:
        sheet = _find_form_sheet(workbook)
        form = RequestForm(source_sheet=sheet.title)
        rows = _sheet_rows(sheet)

        _parse_header(rows, form)
        table_header_idx = _parse_skus(rows, form)
        _parse_signature(rows, form, after_row=table_header_idx)

        requirements_sheet = _find_requirements_sheet(workbook, exclude=sheet)
        if requirements_sheet is not None:
            _parse_requirements(requirements_sheet, form)

        _check(form)
        return form
    finally:
        workbook.close()
