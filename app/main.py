"""dma-rims HTTP API.

DRAFT — requires QA/RA review. Not validated.

One application, no modules, no approval workflow. The shape of every write is
the same: establish the caller, check they may edit, diff the payload against
what is stored, demand a reason, and write the change and its audit records in
one transaction.
"""

from __future__ import annotations

import io
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import audit, files
from .config import Settings, load_settings
from .intake.form_parser import FormParseError, parse_registration_request
from .identity import (
    ChainResolver,
    DatabricksGroupResolver,
    GroupResolver,
    StaticGroupResolver,
    User,
    user_from_headers,
)
from .store import REGISTRATION_EDITABLE, Store

log = logging.getLogger(__name__)

app = FastAPI(title="AirLife Regulatory Affairs", version="0.1.0")

# Set at startup; overridden in tests via dependency_overrides.
_settings: Settings = load_settings()
_resolver: GroupResolver = ChainResolver(
    DatabricksGroupResolver(),
    StaticGroupResolver(_settings.editor_emails, group_name=_settings.editor_group or "editors"),
)
_store: Optional[Store] = None


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------

def get_settings() -> Settings:
    return _settings


_file_store = None


def get_file_store():
    global _file_store
    if _file_store is None:
        _file_store = files.build_file_store(_settings.evidence_volume)
    return _file_store


def get_store() -> Store:
    if _store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No database is configured for this app.",
        )
    return _store


def current_user(
    request: Request, settings: Settings = Depends(get_settings)
) -> User:
    return user_from_headers(
        request.headers,
        _resolver,
        editor_group=settings.editor_group,
        dev_mode=settings.dev_mode,
        dev_user_email=settings.dev_user_email,
    )


def require_editor(user: User = Depends(current_user)) -> User:
    if not user.can_edit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "You have read access to this application. Changing records "
                "requires membership of the Regulatory Affairs editor group."
            ),
        )
    return user


# ---------------------------------------------------------------------------
# payloads
# ---------------------------------------------------------------------------

class ChangeRequest(BaseModel):
    """Every write carries the fields to change and why."""

    reason: str = Field(min_length=audit.MIN_REASON_CHARS)
    changes: Dict[str, Any] = Field(default_factory=dict)


class HoldRequest(BaseModel):
    gtin: str = Field(min_length=8)
    scope: Optional[str] = None
    reason: str = Field(min_length=audit.MIN_REASON_CHARS)
    source_ref: Optional[str] = None


class LiftRequest(BaseModel):
    reason: str = Field(min_length=audit.MIN_REASON_CHARS)


class LicenceGroupInput(BaseModel):
    """A licence application the reviewer is confirming or correcting."""

    note: str = ""
    skus: List[str] = Field(default_factory=list)
    description: str = ""


class AcceptRequest(BaseModel):
    reason: str = Field(min_length=audit.MIN_REASON_CHARS)
    # Omitted means "use the grouping the parser found". Supplied means the
    # reviewer has corrected it — which is the point of showing the warnings.
    groups: Optional[List[LicenceGroupInput]] = None


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    ephemeral = not settings.has_database and settings.dev_mode
    return {
        "status": "ok",
        "database_configured": settings.has_database,
        "evidence_volume_configured": settings.evidence_volume.startswith("/Volumes/"),
        # loud on purpose: nobody should mistake a dev instance for a real one
        "storage": "in-memory (DEV — data is lost on restart)" if ephemeral else "database",
    }


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> Dict[str, Any]:
    return {
        "email": user.email,
        "username": user.username,
        "level": user.level.value,
        "can_edit": user.can_edit,
    }


# ---------------------------------------------------------------------------
# registrations
# ---------------------------------------------------------------------------

@app.get("/api/registrations")
def list_registrations(
    country: Optional[str] = None,
    product_class: Optional[str] = None,
    status_filter: Optional[str] = None,
    search: Optional[str] = None,
    include_archived: bool = False,
    limit: int = 250,
    store: Store = Depends(get_store),
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    rows = store.list_registrations(
        country_code=country,
        product_class_code=product_class,
        status=status_filter,
        search=search,
        include_archived=include_archived,
        limit=min(limit, 1000),
    )
    return {"count": len(rows), "registrations": rows}


@app.get("/api/registrations/{registration_id}")
def get_registration(
    registration_id: int,
    store: Store = Depends(get_store),
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    row = store.get_registration(registration_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such registration.")
    return row


@app.patch("/api/registrations/{registration_id}")
def update_registration(
    registration_id: int,
    payload: ChangeRequest,
    # `user` is declared before `store` deliberately: FastAPI resolves
    # dependencies in parameter order, and a caller who may not edit must be
    # refused whether or not the database is reachable.
    user: User = Depends(require_editor),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    existing = store.get_registration(registration_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="No such registration.")

    try:
        reason = audit.check_reason(payload.reason)
    except audit.ReasonRequired as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    changes = audit.diff(existing, payload.changes, fields=REGISTRATION_EDITABLE)
    if not changes:
        # Not an error: the client sent values identical to what is stored.
        # Writing an audit row here would fill the trail with non-events.
        return {"registration": existing, "changed": [], "audit_written": 0}

    records = audit.records_for_update(
        actor=user.actor,
        entity="registration",
        entity_id=registration_id,
        changes=changes,
        reason=reason,
    )
    applied = {c.field: payload.changes[c.field] for c in changes}
    updated = store.update_registration(registration_id, applied, user.actor, records)

    return {
        "registration": updated,
        "changed": [c.field for c in changes],
        "audit_written": len(records),
    }


@app.get("/api/registrations/{registration_id}/history")
def registration_history(
    registration_id: int,
    store: Store = Depends(get_store),
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    return {"history": store.audit_for("registration", str(registration_id))}


# ---------------------------------------------------------------------------
# GTIN authorizations and holds
# ---------------------------------------------------------------------------

@app.get("/api/gtins")
def list_gtins(
    gtin: Optional[str] = None,
    country: Optional[str] = None,
    store: Store = Depends(get_store),
    user: User = Depends(current_user),
) -> Dict[str, Any]:
    rows = store.list_gtins(gtin=gtin, country_code=country)
    return {"count": len(rows), "gtins": rows}


@app.get("/api/holds")
def list_holds(
    active_only: bool = True,
    user: User = Depends(current_user),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    rows = store.list_holds(active_only=active_only)
    return {"count": len(rows), "holds": rows}


@app.post("/api/holds", status_code=201)
def apply_hold(
    payload: HoldRequest,
    user: User = Depends(require_editor),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    reason = audit.check_reason(payload.reason)
    record = audit.record_for_insert(
        actor=user.actor, entity="gtin_hold", entity_id=payload.gtin, reason=reason
    )
    hold = store.apply_hold(
        gtin=payload.gtin,
        scope=payload.scope,
        reason=reason,
        source_ref=payload.source_ref,
        actor=user.actor,
        records=[record],
    )
    return {"hold": hold}


@app.post("/api/holds/{hold_id}/lift")
def lift_hold(
    hold_id: int,
    payload: LiftRequest,
    user: User = Depends(require_editor),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    reason = audit.check_reason(payload.reason)
    record = audit.record_for_update_single(
        actor=user.actor,
        entity="gtin_hold",
        entity_id=hold_id,
        field="lifted_at",
        old_value=None,
        new_value="lifted",
        reason=reason,
    )
    hold = store.lift_hold(hold_id, user.actor, reason, [record])
    return {"hold": hold}


# ---------------------------------------------------------------------------
# startup
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Registration Request Form intake
# ---------------------------------------------------------------------------
#
# Two steps on purpose. Upload parses the workbook and shows what was read,
# including anything the parser could not decide; accept is a separate,
# deliberate action that creates the registrations. The parser flags ambiguous
# licence grouping precisely because a wrong grouping means a wrong number of
# licence applications, so nothing is created until a person has looked.


@app.post("/api/intake/forms", status_code=201)
async def upload_request_form(
    file: UploadFile = File(...),
    user: User = Depends(require_editor),
    store: Store = Depends(get_store),
    settings: Settings = Depends(get_settings),
) -> Dict[str, Any]:
    raw = await file.read()
    try:
        files.check_size(raw)
    except files.FileTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        form = parse_registration_request(io.BytesIO(raw))
    except FormParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    parsed = form.to_dict()

    country = store.find_country_by_name(form.country) if form.country else None
    warnings = list(parsed["warnings"])
    if form.country and country is None:
        # Not fatal at upload. It becomes fatal at accept, where a registration
        # needs a real country, so the reviewer is told now rather than then.
        warnings.append(
            "Country '%s' does not match any country on file; it must be "
            "resolved before this form can be accepted." % form.country
        )

    relative = files.generate_path("request-forms", file.filename or "form.xlsx")
    try:
        stored_path = get_file_store().write(relative, raw)
    except files.FileStoreError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    record = audit.record_for_insert(
        actor=user.actor,
        entity="request_form",
        entity_id="pending",
        reason="Registration Request Form uploaded: %s" % (file.filename or "form.xlsx"),
    )
    row = store.create_request_form(
        {
            "filename": file.filename or "form.xlsx",
            "volume_path": stored_path,
            "country_name": form.country,
            "country_code": country["code"] if country else None,
            "distributor": form.distributor,
            "ra_contact": form.ra_contact,
            "request_date": form.request_date,
            "licences_expected": form.licences_expected,
            "signed_by": form.signed_by,
            "signature_date": form.signature_date,
            "sku_count": form.sku_count,
            "source_sheet": form.source_sheet,
            "parsed": json.dumps(parsed),
            "warnings": json.dumps(warnings),
        },
        actor=user.actor,
        records=[record],
    )
    return {"form": row, "parsed": parsed, "warnings": warnings}


@app.get("/api/intake/forms")
def list_request_forms(
    state: Optional[str] = None,
    user: User = Depends(current_user),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    rows = store.list_request_forms(state=state)
    return {"count": len(rows), "forms": rows}


@app.get("/api/intake/forms/{form_id}")
def get_request_form(
    form_id: int,
    user: User = Depends(current_user),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    row = store.get_request_form(form_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such request form.")
    return row


@app.post("/api/intake/forms/{form_id}/accept")
def accept_request_form(
    form_id: int,
    payload: AcceptRequest,
    user: User = Depends(require_editor),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    row = store.get_request_form(form_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such request form.")
    if row.get("state") != "parsed":
        raise HTTPException(
            status_code=409, detail="This form has already been %s." % row.get("state")
        )

    parsed = row["parsed"]
    if isinstance(parsed, str):
        parsed = json.loads(parsed)

    country_code = row.get("country_code")
    if not country_code:
        raise HTTPException(
            status_code=422,
            detail=(
                "This form's country (%s) is not on file, so registrations "
                "cannot be raised from it." % (row.get("country_name") or "blank")
            ),
        )

    groups = payload.groups or [
        LicenceGroupInput(
            note=g.get("note", ""),
            skus=g.get("skus", []),
            description=g.get("description", ""),
        )
        for g in parsed.get("groups", [])
    ]
    if not groups:
        raise HTTPException(
            status_code=422, detail="This form produced no licence applications."
        )

    reason = audit.check_reason(payload.reason)
    request_id = audit.new_request_id()

    registrations = []
    for index, group in enumerate(groups, start=1):
        registrations.append(
            {
                "country_code": country_code,
                "sku": ", ".join(group.skus)[:400],
                "product_description": _group_description(group),
                "pending_type": "Initial Registration",
                "local_distributors": row.get("distributor"),
                "company_contact": row.get("ra_contact"),
                "comments": group.note,
                "_licence_group": index,
            }
        )

    records = [
        audit.record_for_insert(
            actor=user.actor,
            entity="request_form",
            entity_id=str(form_id),
            reason=reason,
            request_id=request_id,
        )
    ]

    try:
        created = store.accept_request_form(
            form_id, registrations, user.actor, reason, records
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"created": len(created), "registrations": created}


@app.post("/api/intake/forms/{form_id}/reject")
def reject_request_form(
    form_id: int,
    payload: LiftRequest,
    user: User = Depends(require_editor),
    store: Store = Depends(get_store),
) -> Dict[str, Any]:
    reason = audit.check_reason(payload.reason)
    record = audit.record_for_update_single(
        actor=user.actor, entity="request_form", entity_id=form_id,
        field="state", old_value="parsed", new_value="rejected", reason=reason,
    )
    try:
        row = store.reject_request_form(form_id, user.actor, reason, [record])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"form": row}


def _group_description(group: "LicenceGroupInput") -> str:
    if group.description:
        return group.description[:400]
    return "Licence application covering %d SKU(s)" % len(group.skus)


# ---------------------------------------------------------------------------
# the single-page app
# ---------------------------------------------------------------------------
#
# Mounted after every /api route is declared. The catch-all below returns the
# shell for any non-API path so the hash router owns navigation, but it must
# never swallow an unknown /api path — that should 404 as itself rather than
# returning HTML a fetch() will fail to parse.

if _settings.static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_settings.static_dir)), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(_settings.static_dir / "index.html"))


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="No such endpoint.")
    return FileResponse(str(_settings.static_dir / "index.html"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _store
    if _settings.has_database:
        from .store_pg import PostgresStore

        # Lazy pool: constructing this opens no connection, so an unreachable
        # Lakebase makes the app unhealthy rather than unable to start.
        _store = PostgresStore(_settings.database_url)
    elif _settings.dev_mode:
        from .store_memory import MemoryStore

        _store = MemoryStore()
        log.warning(
            "DEV MODE: using an in-memory store. Everything is lost when this "
            "process stops. Never run a deployed app this way."
        )
    else:
        log.warning("DMA_DATABASE_URL is not set; the API will refuse data requests.")
    yield
    if _store is not None and hasattr(_store, "close"):
        _store.close()


app.router.lifespan_context = lifespan
