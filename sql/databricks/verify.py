"""Check the Databricks SQL against a real warehouse, without creating anything.

Each view and function body is read from the .sql files in this directory and
run as a plain SELECT, with the dma_ tables replaced by inline rows. So this
proves the SQL runs in the warehouse's dialect AND that it gives the right
answers — while touching nothing but a read of cm_country_codes.

    python sql/databricks/verify.py

Needs the Databricks CLI, authenticated. Set DATABRICKS_CONFIG_PROFILE to pick
a profile and DMA_WAREHOUSE_ID to pick a warehouse.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WAREHOUSE = os.environ.get("DMA_WAREHOUSE_ID", "7d04976612124c76")
PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE")
PREFIX = "cdw_prod.glb_bi."


# ---------------------------------------------------------------------------
# warehouse access
# ---------------------------------------------------------------------------

def query(stmt):
    cmd = ["databricks", "api", "post", "/api/2.0/sql/statements", "--json",
           json.dumps({"warehouse_id": WAREHOUSE, "statement": stmt,
                       "wait_timeout": "50s"})]
    if PROFILE:
        cmd += ["-p", PROFILE]
    # Git Bash rewrites /api/... into a Windows path without this
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    out = proc.stdout
    if "{" not in out:
        raise RuntimeError((out or proc.stderr).strip()[:400])
    body = json.loads(out[out.index("{"):])
    status = body.get("status", {})
    if status.get("state") != "SUCCEEDED":
        raise RuntimeError(status.get("error", {}).get("message", str(status))[:800])
    cols = [c["name"] for c in body["manifest"]["schema"]["columns"]]
    return [dict(zip(cols, r)) for r in body.get("result", {}).get("data_array") or []]


# ---------------------------------------------------------------------------
# reading bodies out of the .sql files
# ---------------------------------------------------------------------------

def strip_comments(sql):
    return re.sub(r"--[^\n]*", "", sql)


def view_body(filename, name):
    sql = strip_comments((HERE / filename).read_text())
    m = re.search(r"CREATE OR REPLACE VIEW %s%s AS\s*(.*?);" % (re.escape(PREFIX), name),
                  sql, re.S)
    if not m:
        raise LookupError("view %s not found in %s" % (name, filename))
    return m.group(1)


def status_case():
    sql = strip_comments((HERE / "02_views.sql").read_text())
    m = re.search(r"dma_registration_status\(.*?RETURN\s+(CASE.*?END)\s*;", sql, re.S)
    return m.group(1)


def split_args(text):
    args, depth, cur = [], 0, ""
    for ch in text:
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    args.append(cur.strip())
    return args


def inline_status(sql):
    """Replace each call to the status UDF with its CASE body, arguments
    substituted, so the views can run without the function existing."""
    case = status_case()
    params = ["p_archived", "p_pending_type", "p_in_progress", "p_expiry_date",
              "p_expiring_days"]
    out, pos = "", 0
    needle = PREFIX + "dma_registration_status("
    while True:
        i = sql.find(needle, pos)
        if i < 0:
            return out + sql[pos:]
        j, depth = i + len(needle), 1
        while depth:
            depth += {"(": 1, ")": -1}.get(sql[j], 0)
            j += 1
        args = split_args(sql[i + len(needle):j - 1]) + ["90"]
        body = case
        for p, a in zip(params, args):
            body = re.sub(r"\b%s\b" % p, "(%s)" % a, body)
        out += sql[pos:i] + "(" + body + ")"
        pos = j


def local(sql):
    """Point qualified dma_ names at the inline CTEs, but leave the real
    country source alone."""
    return inline_status(sql).replace(PREFIX + "dma_", "dma_")


# ---------------------------------------------------------------------------
# sample data
# ---------------------------------------------------------------------------

def d(days):
    return "date_add(current_date(), %d)" % days


NULL = "NULL"
NOW = "current_timestamp()"

REGISTRATION_COLS = [
    ("id", "BIGINT"), ("registration_number", "STRING"), ("country_code", "STRING"),
    ("expiry_date", "DATE"), ("pending_type", "STRING"), ("in_progress", "BOOLEAN"),
    ("archived", "BOOLEAN"), ("alert_months", "INT"), ("updated_at", "TIMESTAMP"),
]
REGISTRATIONS = [
    # id   number   ctry  expiry    pending                 inprog   archived  alert
    ("101", "'R-1'", "'MY'", d(-5),  NULL,                    "false", "false", "12", NOW),
    ("102", "'R-2'", "'MY'", d(30),  NULL,                    "false", "false", "12", NOW),
    ("103", NULL,    "'MY'", NULL,   "'Initial Registration'", "false", "false", "12", NOW),
    ("104", "'R-4'", "'MY'", d(400), NULL,                    "false", "false", "12", NOW),
]

GTIN_COLS = [
    ("id", "BIGINT"), ("gtin", "STRING"), ("sku", "STRING"), ("country_code", "STRING"),
    ("status_for_sale", "STRING"), ("shippable", "BOOLEAN"), ("expiry_date", "DATE"),
    ("registration_id", "BIGINT"), ("reason_comments", "STRING"),
    ("license_number", "STRING"), ("is_kit", "BOOLEAN"), ("item_description", "STRING"),
    ("vendor", "STRING"), ("source_system", "STRING"), ("updated_at", "TIMESTAMP"),
]


def gtin(i, code, country, sale, ship, expiry=NULL, reg=NULL):
    return (str(i), "'%s'" % code, "'SKU%d'" % i, "'%s'" % country, "'%s'" % sale,
            ship, expiry, reg, NULL, NULL, "false", NULL, NULL, "'test'", NOW)


GTINS = [
    gtin(1, "G1", "MY", "Approved", "true"),                        # ships
    gtin(2, "G2", "MY", "Approved", "true", expiry=d(-10)),         # auth expired
    gtin(3, "G3", "MY", "Approved", "true"),                        # two holds
    gtin(4, "G4", "MY", "Not Approved", "true"),                    # not approved
    gtin(5, "G5", "MY", "Approved", "true", reg="101"),             # reg expired
    gtin(6, "G6", "MY", "Approved", "true", expiry=d(30)),          # expiring
    gtin(7, "G7", "MY", "Approved", "true"),                        # hold lifted
    gtin(8, "G8", "ZZ", "Approved", "false"),                       # unknown country
    gtin(9, "G9", "MY", "Approved", "true"),                        # hold elsewhere
]

HOLD_COLS = [
    ("id", "BIGINT"), ("gtin", "STRING"), ("scope", "STRING"), ("reason", "STRING"),
    ("source_ref", "STRING"), ("applied_at", "TIMESTAMP"), ("lifted_at", "TIMESTAMP"),
]
HOLDS = [
    ("1", "'G3'", NULL,   "'global hold'",  "'CAPA-1'", NOW, NULL),
    ("2", "'G3'", "'MY'", "'country hold'", "'NCR-2'",  NOW, NULL),
    ("3", "'G7'", NULL,   "'old hold'",     NULL,       NOW, NOW),
    ("4", "'G9'", "'SG'", "'SG only'",      NULL,       NOW, NULL),
]

HISTORY_COLS = [
    ("id", "BIGINT"), ("run_key", "STRING"), ("recorded_at", "TIMESTAMP"),
    ("gtin", "STRING"), ("sku", "STRING"), ("country_code", "STRING"),
    ("is_shippable", "BOOLEAN"), ("block_reason", "STRING"), ("detail", "STRING"),
]


def ts(days):
    return "current_timestamp() + INTERVAL %d DAY" % days


HISTORY = [
    # G1 was on hold two days ago, then cleared yesterday: latest says ships
    ("1", "'r0'", ts(-2), "'G1'", NULL, "'MY'", "false", "'ON HOLD'", NULL),
    ("2", "'r1'", ts(-1), "'G1'", NULL, "'MY'", "true",  NULL,        NULL),
    # G2 already recorded as expired: no change
    ("3", "'r1'", ts(-1), "'G2'", NULL, "'MY'", "false", "'AUTHORIZATION EXPIRED'", NULL),
    # G3 recorded as shippable, now on hold: a change
    ("4", "'r1'", ts(-1), "'G3'", NULL, "'MY'", "true",  NULL,        NULL),
]


def cte(name, cols, rows):
    if not rows:
        sel = ", ".join("CAST(NULL AS %s) AS %s" % (t, c) for c, t in cols)
        return "%s AS (SELECT %s WHERE false)" % (name, sel)
    parts = []
    for row in rows:
        parts.append("SELECT " + ", ".join(
            "CAST(%s AS %s) AS %s" % (v, t, c) for (c, t), v in zip(cols, row)))
    return "%s AS (%s)" % (name, " UNION ALL ".join(parts))


def fixtures(history=HISTORY):
    return [
        "dma_country AS (%s)" % view_body("01_tables.sql", "dma_country"),
        cte("dma_registration", REGISTRATION_COLS, REGISTRATIONS),
        cte("dma_gtin_authorization", GTIN_COLS, GTINS),
        cte("dma_gtin_hold", HOLD_COLS, HOLDS),
        cte("dma_export_eligibility_history", HISTORY_COLS, history),
    ]


def with_views(*views, history=HISTORY):
    """WITH clause holding the fixtures plus the named views, in order."""
    parts = fixtures(history)
    for filename, name in views:
        parts.append("%s AS (%s)" % (name, local(view_body(filename, name))))
    return "WITH " + ",\n".join(parts) + "\n"


ELIG = ("02_views.sql", "dma_vw_gtin_country_eligibility")


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

failures = []


def check(label, actual, expected):
    ok = actual == expected
    print("  %s %s" % ("ok  " if ok else "FAIL", label))
    if not ok:
        print("       expected %r\n       got      %r" % (expected, actual))
        failures.append(label)


def section(title):
    print("\n" + title)


def main():
    section("dma_country (real cm_country_codes)")
    rows = query("WITH " + fixtures()[0] + " SELECT count(*) n, "
                 "count_if(code = 'MY') my FROM dma_country")
    check("has rows", int(rows[0]["n"]) > 0, True)
    check("knows MY, used by the fixtures", rows[0]["my"], "1")

    section("dma_registration_current")
    rows = query(with_views(("02_views.sql", "dma_registration_current"))
                 + "SELECT id, status, in_alert_window, country_name "
                   "FROM dma_registration_current ORDER BY id")
    by = {r["id"]: r for r in rows}
    check("101 expired 5 days ago -> Expired", by["101"]["status"], "Expired")
    check("102 expires in 30 days -> Expiring soon", by["102"]["status"], "Expiring soon")
    check("103 pending -> Pending", by["103"]["status"], "Pending")
    check("104 expires in 400 days -> Active", by["104"]["status"], "Active")
    check("102 is inside a 12-month alert window", by["102"]["in_alert_window"], "true")
    check("104 is outside a 12-month alert window", by["104"]["in_alert_window"], "false")
    check("101 has expired, so not in the alert window", by["101"]["in_alert_window"], "false")
    check("country name joined", by["101"]["country_name"] is not None, True)

    section("dma_vw_gtin_country_eligibility")
    rows = query(with_views(ELIG) +
                 "SELECT gtin, is_shippable, block_reason, hold_reason, country_name "
                 "FROM dma_vw_gtin_country_eligibility ORDER BY gtin")
    check("one row per GTIN x country (nine in, nine out)", len(rows), 9)
    by = {r["gtin"]: r for r in rows}
    check("G1 approved, no hold -> ships", (by["G1"]["is_shippable"], by["G1"]["block_reason"]), ("true", None))
    check("G2 authorization expired", by["G2"]["block_reason"], "AUTHORIZATION EXPIRED")
    check("G3 global + country hold -> one row, ON HOLD", by["G3"]["block_reason"], "ON HOLD")
    check("G3 most recent hold wins", by["G3"]["hold_reason"] in ("global hold", "country hold"), True)
    check("G4 not approved", by["G4"]["block_reason"], "NOT APPROVED FOR SALE")
    check("G5 registration expired", by["G5"]["block_reason"], "REGISTRATION EXPIRED")
    check("G6 expires in 30 days -> still ships", by["G6"]["is_shippable"], "true")
    check("G7 lifted hold -> ships", by["G7"]["is_shippable"], "true")
    check("G8 unknown country is NOT dropped", "G8" in by, True)
    check("G8 unknown country, blocked", by.get("G8", {}).get("block_reason"), "BLOCKED")
    check("G8 unknown country has no name", by.get("G8", {}).get("country_name"), None)
    check("G9 hold scoped to another country -> ships", by["G9"]["is_shippable"], "true")

    section("dma_vw_gtin_country_blocked")
    rows = query(with_views(ELIG, ("02_views.sql", "dma_vw_gtin_country_blocked")) +
                 "SELECT gtin, detail FROM dma_vw_gtin_country_blocked ORDER BY gtin")
    check("exactly the blocked GTINs", [r["gtin"] for r in rows],
          ["G2", "G3", "G4", "G5", "G8"])

    section("dma_vw_gtin_country_expiring")
    rows = query(with_views(ELIG, ("02_views.sql", "dma_vw_gtin_country_expiring")) +
                 "SELECT gtin, days_remaining FROM dma_vw_gtin_country_expiring")
    check("only G6, 30 days out", [(r["gtin"], r["days_remaining"]) for r in rows],
          [("G6", "30")])

    section("04_nightly_job.sql statement 2 (record changes)")
    job = strip_comments((HERE / "04_nightly_job.sql").read_text())
    insert = [s for s in job.split(";") if "dma_export_eligibility_history (" in s][0]
    select = insert[insert.index("WITH current_state"):].replace(":run_key", "'test'")
    select = local(select).replace("WITH current_state", "current_state", 1)
    rows = query(_job_select(select))
    got = sorted(r["gtin"] for r in rows)
    check("G1 unchanged (latest of two history rows), G2 unchanged; rest recorded",
          got, ["G3", "G4", "G5", "G6", "G7", "G8", "G9"])
    rows = query(_job_select(select, history=[]))
    check("first ever run records every row", len(rows), 9)

    section("04_nightly_job.sql statement 3 (run figures)")
    merge = [s for s in job.split(";") if "MERGE INTO" in s][0]
    using = merge[merge.index("USING (") + len("USING ("):merge.index(") AS s")]
    using = local(using.replace(":run_key", "'r1'"))
    rows = query(with_views(ELIG) + using)
    r = rows[0]
    check("totals", (r["total_rows"], r["shippable_rows"], r["blocked_rows"]), ("9", "4", "5"))
    check("changed_rows counts this run's history rows", r["changed_rows"], "3")

    section("dma_eligibility_as_of")
    fn = strip_comments((HERE / "03_export_snapshot.sql").read_text())
    body = re.search(r"dma_eligibility_as_of\(.*?RETURN\s+(SELECT.*?)\s*;", fn, re.S).group(1)
    body = local(body).replace("p_when", "current_timestamp() + INTERVAL -36 HOUR")
    rows = query(with_views() + body)
    check("36h ago, G1 still read ON HOLD", [(r["gtin"], r["block_reason"]) for r in rows],
          [("G1", "ON HOLD")])

    print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED" % len(failures)))
    return 1 if failures else 0


def _job_select(select, history=HISTORY):
    head = with_views(ELIG, history=history)
    # the job's own WITH becomes more CTEs on the end of ours
    return head.rstrip("\n") + ",\n" + select


if __name__ == "__main__":
    sys.exit(main())
