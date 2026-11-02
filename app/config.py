"""Settings, read from the environment.

Databricks Apps supplies configuration through `app.yaml` env entries and
secret references, so everything here comes from os.environ with no file
loading and no defaults that would silently work in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional


def _split(value: Optional[str]) -> FrozenSet[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    evidence_volume: str
    editor_group: str
    editor_emails: FrozenSet[str]
    static_dir: Path
    dev_mode: bool
    dev_user_email: str

    @property
    def has_database(self) -> bool:
        return bool(self.database_url)


def load_settings(environ=None) -> Settings:
    env = os.environ if environ is None else environ
    here = Path(__file__).resolve().parent

    dev_mode = env.get("DMA_DEV_MODE", "").lower() in ("1", "true", "yes")

    return Settings(
        database_url=env.get("DMA_DATABASE_URL", ""),
        # e.g. /Volumes/<catalog>/<schema>/evidence
        evidence_volume=env.get("DMA_EVIDENCE_VOLUME", ""),
        # Databricks workspace group whose members may change things
        editor_group=env.get("DMA_EDITOR_GROUP", ""),
        # Fallback / local-dev editor list, comma separated
        editor_emails=_split(env.get("DMA_EDITOR_EMAILS")),
        static_dir=Path(env.get("DMA_STATIC_DIR", str(here / "static"))),
        dev_mode=dev_mode,
        dev_user_email=env.get("DMA_DEV_USER", "dev@localhost"),
    )
