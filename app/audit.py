"""Field-level change records.

Nothing gates a change in this system, so this is the only record of who
changed what and why. Three things follow from that:

  * A change with no reason is rejected before it reaches the database. The
    reason is part of the write, not an optional annotation on it.
  * Values are normalised to text before comparison, so a date arriving as a
    string does not read as a change from the same date held as a `date`.
  * Every field of one user action shares a request_id, so the trail can be
    read back as "this person did this" rather than as loose field edits.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, List, Mapping, Optional, Sequence

MIN_REASON_CHARS = 3


class ReasonRequired(ValueError):
    """A change arrived without a usable reason."""


@dataclass(frozen=True)
class FieldChange:
    field: str
    old_value: Optional[str]
    new_value: Optional[str]


@dataclass(frozen=True)
class AuditRecord:
    actor: str
    action: str           # insert | update | delete
    entity: str
    entity_id: str
    field: Optional[str]
    old_value: Optional[str]
    new_value: Optional[str]
    reason: str
    request_id: str
    occurred_at: _dt.datetime


def normalise(value: Any) -> Optional[str]:
    """Render a value the way it will be compared and stored.

    None and empty string are the same absence: a field cleared to "" should
    not read as a change when it was already NULL.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _dt.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text if text else None


def check_reason(reason: Optional[str]) -> str:
    """Reasons are mandatory. Whitespace and 'x' are not reasons."""
    cleaned = (reason or "").strip()
    if len(cleaned) < MIN_REASON_CHARS:
        raise ReasonRequired(
            "A reason of at least %d characters is required for every change."
            % MIN_REASON_CHARS
        )
    return cleaned


def diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    fields: Optional[Sequence[str]] = None,
) -> List[FieldChange]:
    """Changed fields only.

    `fields` restricts the comparison to the columns a caller is allowed to
    change, so an unexpected key in the payload cannot quietly become an audit
    row for something that was never written.
    """
    keys: Iterable[str] = fields if fields is not None else after.keys()
    changes: List[FieldChange] = []
    for key in keys:
        if key not in after:
            continue
        old = normalise(before.get(key))
        new = normalise(after.get(key))
        if old != new:
            changes.append(FieldChange(field=key, old_value=old, new_value=new))
    return changes


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def records_for_update(
    actor: str,
    entity: str,
    entity_id: Any,
    changes: Sequence[FieldChange],
    reason: str,
    request_id: Optional[str] = None,
) -> List[AuditRecord]:
    reason = check_reason(reason)
    rid = request_id or new_request_id()
    stamp = _now()
    return [
        AuditRecord(
            actor=actor,
            action="update",
            entity=entity,
            entity_id=str(entity_id),
            field=change.field,
            old_value=change.old_value,
            new_value=change.new_value,
            reason=reason,
            request_id=rid,
            occurred_at=stamp,
        )
        for change in changes
    ]


def record_for_update_single(
    actor: str,
    entity: str,
    entity_id: Any,
    field: str,
    old_value: Optional[str],
    new_value: Optional[str],
    reason: str,
    request_id: Optional[str] = None,
) -> AuditRecord:
    """One field changed by a purpose-built action rather than a generic PATCH."""
    return AuditRecord(
        actor=actor,
        action="update",
        entity=entity,
        entity_id=str(entity_id),
        field=field,
        old_value=normalise(old_value),
        new_value=normalise(new_value),
        reason=check_reason(reason),
        request_id=request_id or new_request_id(),
        occurred_at=_now(),
    )


def record_for_insert(
    actor: str,
    entity: str,
    entity_id: Any,
    reason: str,
    request_id: Optional[str] = None,
) -> AuditRecord:
    return _single("insert", actor, entity, entity_id, reason, request_id)


def record_for_delete(
    actor: str,
    entity: str,
    entity_id: Any,
    reason: str,
    request_id: Optional[str] = None,
) -> AuditRecord:
    return _single("delete", actor, entity, entity_id, reason, request_id)


def _single(
    action: str,
    actor: str,
    entity: str,
    entity_id: Any,
    reason: str,
    request_id: Optional[str],
) -> AuditRecord:
    return AuditRecord(
        actor=actor,
        action=action,
        entity=entity,
        entity_id=str(entity_id),
        field=None,
        old_value=None,
        new_value=None,
        reason=check_reason(reason),
        request_id=request_id or new_request_id(),
        occurred_at=_now(),
    )


def new_request_id() -> str:
    return uuid.uuid4().hex
