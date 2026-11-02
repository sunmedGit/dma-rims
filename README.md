# dma-rims

Regulatory Affairs registration and GTIN authorization system, built as a
Databricks App.

> **DRAFT — requires QA/RA review.** Nothing in this repository is validated.
> The system must go through AirLife's software validation process before it is
> used for regulatory decisions, and no output of it is a controlled record.

## What this is

One application (not two modules) covering:

- Country registration records, expiry tracking and renewal alerts
- GTIN / SKU / country authorization and shipping eligibility
- Holds, recorded as events rather than a flag
- Customer and Market Authorization Holder profiles, with approval evidence
- Intake of completed **Registration Request Forms** (.xlsx)

Changes apply immediately — there is no approval workflow. Because nothing
gates a change, the audit trail is the only record of who changed what, so it
is not optional.

## Status

| Piece | State |
|---|---|
| Registration Request Form reader | Built, 23 tests passing |
| Database schema | Written, parse-validated, **not yet run** |
| HTTP API | Not started |
| Front end | Not started — ported from the reviewed prototype |
| Databricks App packaging | Not started |

Decisions settled so far:

- **No approval workflow.** Changes apply immediately. Each one carries a
  required reason, captured in `audit_log`.
- **Two access levels**, Editor and Viewer, mapped from Databricks workspace
  groups. Enforced in the API, not the database — see `sql/grants.sql`.

## Layout

```
app/
  intake/form_parser.py   Registration Request Form (.xlsx) reader
sql/                      Lakebase schema (hand over to run; see below)
tests/                    pytest suite; fixtures are built in code
```

## Running the tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## Handling real forms

Completed forms carry distributor contact numbers and signatory names. They are
git-ignored and must not be committed. The test fixtures are built in code with
invented values that mirror the real layout.

## Provisioning

Databricks objects (Lakebase instance, Unity Catalog volumes, secret scopes)
are **not** created from this repository. The SQL and CLI commands are written
out under `sql/` to be reviewed and run by someone with the rights to do it.

## Target runtime

Python 3.11. Avoid 3.12-only syntax — the app servers run 3.11.
