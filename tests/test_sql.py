"""Static validation of the SQL.

None of these scripts can be run here, so this is what stands in for executing
them: the real PostgreSQL grammar (via pglast), plus checks on the things that
would be expensive to discover after the schema is live.

This does not substitute for running them against a database. It catches
syntax and references, not semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pglast import ast, parse_plpgsql, parse_sql

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SQL_FILES = sorted(SQL_DIR.glob("*.sql"))


def read(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    # psql meta-commands and :"var" interpolation are client-side
    text = "\n".join(l for l in text.splitlines() if not l.startswith("\\"))
    return text.replace(':"app_principal"', "app_principal")


def statements(path: Path):
    return parse_sql(read(path))


def strip_comments(text: str) -> str:
    """Drop `-- ...` so prose about SQL is not mistaken for SQL."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def walk(node, on_column_ref, on_alias):
    if isinstance(node, ast.ColumnRef):
        # `r.*` has fields [String('r'), A_Star()]; the alias is not a column
        if node.fields and isinstance(node.fields[-1], ast.A_Star):
            return
        parts = [f.sval for f in node.fields if isinstance(f, ast.String)]
        if parts:
            on_column_ref(parts[-1])
        return
    if isinstance(node, ast.ResTarget) and node.name:
        on_alias(node.name)
    if isinstance(node, (list, tuple)):
        for item in node:
            walk(item, on_column_ref, on_alias)
    elif isinstance(node, ast.Node):
        for slot in node.__slots__:
            walk(getattr(node, slot, None), on_column_ref, on_alias)


@pytest.fixture(scope="module")
def catalog():
    """Everything the scripts define, collected once."""
    columns, tables, views, functions, rules = set(), {}, set(), set(), set()
    for path in SQL_FILES:
        for raw in statements(path):
            st = raw.stmt
            if isinstance(st, ast.CreateStmt):
                own = tables.setdefault(st.relation.relname, set())
                for el in st.tableElts or []:
                    if isinstance(el, ast.ColumnDef):
                        columns.add(el.colname)
                        own.add(el.colname)
            elif isinstance(st, ast.ViewStmt):
                views.add(st.view.relname)
                walk(st.query, lambda _c: None, columns.add)
            elif isinstance(st, ast.CreateFunctionStmt):
                functions.add(st.funcname[-1].sval)
                for p in st.parameters or []:
                    if getattr(p, "name", None):
                        columns.add(p.name)
                for ret in getattr(st, "returnType", None) and [] or []:
                    pass
            elif isinstance(st, ast.RuleStmt):
                rules.add(st.rulename)
    return {
        "columns": columns, "tables": tables, "views": views,
        "functions": functions, "rules": rules,
    }


# --------------------------------------------------------------------------
# grammar
# --------------------------------------------------------------------------

def test_there_are_sql_files():
    assert SQL_FILES, "no .sql files found"


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_each_file_parses(path):
    assert statements(path), "%s parsed to nothing" % path.name


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.name)
def test_each_file_is_transactional(path):
    """Every script wraps itself, so a failure half way leaves nothing behind."""
    text = read(path).upper()
    if "CREATE" not in text and "GRANT" not in text:
        pytest.skip("nothing to wrap")
    assert "BEGIN;" in text and "COMMIT;" in text


def test_plpgsql_function_bodies_parse():
    """The SQL grammar sees a function body as an opaque string; this does not."""
    checked = 0
    for path in SQL_FILES:
        text = read(path)
        for raw in statements(path):
            st = raw.stmt
            if not isinstance(st, ast.CreateFunctionStmt):
                continue
            language = next(
                (o.arg.sval for o in (st.options or [])
                 if o.defname == "language" and isinstance(o.arg, ast.String)),
                None,
            )
            if language != "plpgsql":
                continue
            start = text.index("CREATE OR REPLACE FUNCTION %s" % st.funcname[-1].sval)
            end = text.index("$$;", start) + 3
            tree = parse_plpgsql(text[start:end])
            assert json.dumps(tree), st.funcname[-1].sval
            checked += 1
    assert checked >= 1, "expected at least one plpgsql function"


# --------------------------------------------------------------------------
# references
# --------------------------------------------------------------------------

def test_every_referenced_column_is_defined_somewhere(catalog):
    """Catches a typo in a view that would only surface when it is created."""
    referenced, aliases = set(), set()
    for path in SQL_FILES:
        for raw in statements(path):
            walk(raw.stmt, referenced.add, aliases.add)

    known = catalog["columns"] | aliases
    unresolved = sorted(r for r in referenced if r not in known)
    assert not unresolved, "columns referenced but never defined: %s" % unresolved


def test_the_integration_views_exist(catalog):
    for name in (
        "vw_gtin_country_eligibility",
        "vw_gtin_country_blocked",
        "vw_gtin_country_expiring",
        "vw_export_run_log",
    ):
        assert name in catalog["views"], name


def test_the_export_functions_exist(catalog):
    assert "record_export_run" in catalog["functions"]
    assert "eligibility_as_of" in catalog["functions"]


# --------------------------------------------------------------------------
# the guarantees that matter
# --------------------------------------------------------------------------

def test_registration_status_is_never_stored(catalog):
    """It must be derived. A stored one goes stale the day a licence lapses.

    Other tables may legitimately hold a `status` — a request form's review
    state, an export run's outcome — so this is about `registration` alone.
    """
    assert "status" not in catalog["tables"]["registration"], (
        "registration has a stored 'status' column; it must be derived from "
        "the expiry date instead"
    )


def test_registration_status_is_stable_not_immutable():
    """IMMUTABLE would let the planner cache a result across days."""
    text = strip_comments(read(SQL_DIR / "schema.sql"))
    body = text[text.index("FUNCTION registration_status"):]
    body = body[: body.index("$$;")]
    assert "STABLE" in body
    assert "IMMUTABLE" not in body


def test_shipping_eligibility_accounts_for_expiry():
    """The gap that would release product on a lapsed registration."""
    text = strip_comments(read(SQL_DIR / "views_integration.sql"))
    eligibility = text[text.index("CREATE OR REPLACE VIEW vw_gtin_country_eligibility"):]
    eligibility = eligibility[: eligibility.index("COMMENT ON VIEW")]
    assert "expiry_date >= current_date" in eligibility
    assert "r.expiry_date IS NULL OR r.expiry_date >= current_date" in eligibility


def test_the_hold_lookup_cannot_duplicate_a_row():
    """A global hold and a country hold on one GTIN must not emit two rows."""
    text = strip_comments(read(SQL_DIR / "views_integration.sql"))
    assert "LEFT JOIN LATERAL" in text
    lateral = text[text.index("LEFT JOIN LATERAL"):]
    assert "LIMIT 1" in lateral[: lateral.index(") hold")]


def test_append_only_tables_have_rules(catalog):
    """Audit and export history are evidence; nothing may rewrite them."""
    for rule in (
        "audit_log_no_update", "audit_log_no_delete",
        "export_history_no_update", "export_history_no_delete",
    ):
        assert rule in catalog["rules"], rule


def test_the_export_delta_uses_is_distinct_from():
    """block_reason is NULL when shippable; <> would drop the change silently."""
    text = strip_comments(read(SQL_DIR / "export_snapshot.sql"))
    delta = text[text.index("WHERE l.gtin IS NULL"):]
    delta = delta[: delta.index("RETURNING 1")]
    assert delta.count("IS DISTINCT FROM") == 2
