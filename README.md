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
| Registration Request Form reader | Built, verified against a real form |
| Database schema | Written, parse-validated, **not yet run** |
| HTTP API | Registrations, holds, form intake — 115 tests |
| Front end | Ported: home, registrations, GTIN lookup, holds, intake |
| Databricks App packaging | `app.yaml` written, **not yet deployed** |

## Running it locally, with no database

```bash
python -m pip install -r requirements-dev.txt
DMA_DEV_MODE=1 \
DMA_DEV_USER=you@myairlife.com \
DMA_EDITOR_EMAILS=you@myairlife.com \
DMA_EVIDENCE_VOLUME=./_local_files \
python -m uvicorn app.main:app --port 8000
```

Dev mode uses an in-memory store and the local filesystem. **Everything is
lost when the process stops**, `/api/health` says so, and it is only ever
selected when `DMA_DEV_MODE` is on *and* no database is configured.

Upload a completed form:

```bash
curl -X POST http://127.0.0.1:8000/api/intake/forms -F "file=@/path/to/form.xlsx"
```

## Nightly export

`sql/views_integration.sql` is the contract for a scheduled job. Treat the
column lists as an interface: adding columns is safe, renaming or removing
them breaks the consumer.

| View | What it is for |
|---|---|
| `vw_gtin_country_eligibility` | One row per GTIN × country: may it ship, and if not why |
| `vw_gtin_country_blocked` | Just the blocked rows — the export payload |
| `vw_gtin_country_expiring` | Shippable today, expiring within 90 days |

Three things to settle before building the job:

1. **The answer depends on when you read it.** It is computed from
   `current_date`, so two runs can differ with nothing having changed. Stamp
   the run time onto whatever is exported.
2. **A nightly cadence leaves a gap.** A hold applied at 09:00 does not reach
   the downstream systems until that night. If same-day holds must stop
   shipment, this needs a trigger on hold insert, not a nightly read.
3. **Decide how the consumer behaves on a stale or missing file.** The export
   is a *block* list rather than an allow list for this reason: a failed or
   empty run suppresses nothing instead of releasing everything.

The job should read through its own principal with `SELECT` on these views and
nothing else, so an export can never write.

## Form intake

Upload and accept are deliberately separate. Upload reads the workbook and
reports what it found, including anything it could not decide. Accept is a
second, explicit action that creates one pending registration per licence
application, and takes an optional corrected grouping — which is the answer to
the parser's grouping warnings. A form can only be accepted once.

Not ported yet, because nothing backs them: the world map dashboard,
customer / MAH profiles, approval evidence, EUDAMED, entities and accounts,
the data dictionary and review timelines. The home page lists them rather than
showing sample data that looks real.

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
