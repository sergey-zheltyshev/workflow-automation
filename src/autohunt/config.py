"""Central, offline-safe runtime paths for AutoHunt 2.0."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    project_root: Path
    database_path: Path
    audit_log_path: Path

    @classmethod
    def from_project_root(cls, project_root: Path) -> "Settings":
        root = project_root.resolve()
        return cls(
            project_root=root,
            database_path=root / "var" / "autohunt.sqlite",
            audit_log_path=root / "var" / "audit.jsonl",
        )
