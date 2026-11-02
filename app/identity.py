"""Who is calling, and may they change anything.

Databricks Apps terminates sign-on and forwards the caller's identity in
`X-Forwarded-*` headers. Those headers are only trustworthy because the app is
reachable solely through the Databricks front door — nothing here should be
exposed directly, or the headers become spoofable.

Access has two levels. Editor is granted by membership of a named Databricks
workspace group; everyone else reads. Group lookup is pluggable so the API can
be tested without a workspace.

Resolution fails CLOSED: if group membership cannot be determined, the caller
is a Viewer. A workspace hiccup makes the app read-only, which is the right
way round for a system holding quality records.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet, Optional, Protocol

log = logging.getLogger(__name__)

HEADER_EMAIL = "X-Forwarded-Email"
HEADER_USER = "X-Forwarded-User"
HEADER_USERNAME = "X-Forwarded-Preferred-Username"
HEADER_TOKEN = "X-Forwarded-Access-Token"

DEFAULT_EDITOR_GROUP = "editors"


class AccessLevel(str, Enum):
    VIEWER = "viewer"
    EDITOR = "editor"


@dataclass(frozen=True)
class User:
    email: str
    username: str
    user_id: str
    level: AccessLevel

    @property
    def can_edit(self) -> bool:
        return self.level is AccessLevel.EDITOR

    @property
    def actor(self) -> str:
        """What gets written to the audit trail."""
        return self.email or self.username or self.user_id or "unknown"


class GroupResolver(Protocol):
    def groups_for(self, user_id: str, email: str, token: Optional[str]) -> FrozenSet[str]:
        ...


class StaticGroupResolver:
    """Editors listed by email. For local development and as a fallback.

    Keeping this alongside the workspace resolver means the app can run on a
    laptop without a Databricks connection, and gives an escape hatch if group
    lookup is unavailable on the day.
    """

    def __init__(self, editor_emails: FrozenSet[str], group_name: str = DEFAULT_EDITOR_GROUP):
        self._editors = frozenset(e.lower() for e in editor_emails)
        self._group = group_name

    def groups_for(self, user_id: str, email: str, token: Optional[str]) -> FrozenSet[str]:
        if email and email.lower() in self._editors:
            return frozenset({self._group})
        return frozenset()


class DatabricksGroupResolver:
    """Workspace group membership via the Databricks SDK.

    Results are cached per user for the life of the process; group changes take
    effect on the next app restart. That is a deliberate trade — a SCIM call on
    every request would put workspace availability in the path of every page.
    """

    def __init__(self, workspace_client=None):
        self._client = workspace_client
        self._cache: dict = {}

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from databricks.sdk import WorkspaceClient  # imported lazily
        except ImportError:
            log.warning("databricks-sdk not installed; group lookup unavailable")
            return None
        try:
            self._client = WorkspaceClient()
        except Exception as exc:
            log.warning("Could not create a Databricks workspace client: %s", exc)
            return None
        return self._client

    def groups_for(self, user_id: str, email: str, token: Optional[str]) -> FrozenSet[str]:
        key = user_id or email
        if key in self._cache:
            return self._cache[key]

        client = self._get_client()
        if client is None:
            return frozenset()

        try:
            user = None
            if user_id:
                user = client.users.get(user_id)
            elif email:
                matches = list(client.users.list(filter='userName eq "%s"' % email))
                user = matches[0] if matches else None
            groups = frozenset(
                (g.display or "").lower() for g in (getattr(user, "groups", None) or [])
            )
        except Exception as exc:
            # fail closed: unknown membership means viewer
            log.warning("Group lookup failed for %s: %s", key, exc)
            return frozenset()

        self._cache[key] = groups
        return groups


class ChainResolver:
    """Try each resolver in turn; the first non-empty answer wins."""

    def __init__(self, *resolvers: GroupResolver):
        self._resolvers = resolvers

    def groups_for(self, user_id: str, email: str, token: Optional[str]) -> FrozenSet[str]:
        for resolver in self._resolvers:
            groups = resolver.groups_for(user_id, email, token)
            if groups:
                return groups
        return frozenset()


def user_from_headers(
    headers,
    resolver: GroupResolver,
    editor_group: str,
    dev_mode: bool = False,
    dev_user_email: str = "dev@localhost",
) -> User:
    """Build the calling user from the forwarded headers.

    In dev mode (local only) the headers are usually absent, so a configured
    identity stands in. Dev mode must never be enabled on a deployed app.
    """

    def head(name: str) -> str:
        value = headers.get(name)
        return value.strip() if value else ""

    email = head(HEADER_EMAIL)
    username = head(HEADER_USERNAME)
    user_id = head(HEADER_USER)
    token = head(HEADER_TOKEN) or None

    if dev_mode and not (email or username or user_id):
        email = dev_user_email
        username = dev_user_email

    groups = resolver.groups_for(user_id, email, token)
    # An unconfigured editor group falls back to the StaticGroupResolver's own
    # name, so a laptop with DMA_EDITOR_EMAILS set still works. With neither
    # configured, no resolver returns anything and everyone is a Viewer.
    wanted = (editor_group or DEFAULT_EDITOR_GROUP).lower()
    level = AccessLevel.EDITOR if wanted in groups else AccessLevel.VIEWER
    return User(email=email, username=username, user_id=user_id, level=level)
