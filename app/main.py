"""dma-rims HTTP API.

DRAFT — requires QA/RA review. Not validated.

One application, no modules, no approval workflow. The shape of every write is
the same: establish the caller, check they may edit, diff the payload against
what is stored, demand a reason, and write the change and its audit records in
one transaction.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import audit
from .config import Settings, load_settings
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


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    return {
        "status": "ok",
        "database_configured": settings.has_database,
        "evidence_volume_configured": bool(settings.evidence_volume),
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

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _store
    if _settings.has_database:
        from .store_pg import PostgresStore

        # Lazy pool: constructing this opens no connection, so an unreachable
        # Lakebase makes the app unhealthy rather than unable to start.
        _store = PostgresStore(_settings.database_url)
    else:
        log.warning("DMA_DATABASE_URL is not set; the API will refuse data requests.")
    yield
    if _store is not None and hasattr(_store, "close"):
        _store.close()


app.router.lifespan_context = lifespan
