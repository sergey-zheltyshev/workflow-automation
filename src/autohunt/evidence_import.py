"""Import immutable Phase 3 manifests into the local registry."""

import json
from pathlib import Path
from typing import Any

from .models import ChatThreadDTO, VacancyDTO
from .sync import RegistrySync


def _records(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key in ("search", "first_page", "second_page", "chat_list"):
        value = manifest.get(key)
        if isinstance(value, dict):
            records.append(value)
    records.extend(value for value in manifest.get("vacancies", []) if isinstance(value, dict))
    records.extend(value for value in manifest.get("threads", []) if isinstance(value, dict))
    return records


def import_phase3_evidence(database: Path, evidence_root: Path) -> dict[str, int]:
    sync = RegistrySync(database)
    imported = {"manifests": 0, "vacancies": 0, "threads": 0, "snapshots": 0}
    for manifest_path in sorted(evidence_root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        imported["manifests"] += 1
        snapshot_rows = []
        for record in _records(manifest):
            if record.get("sha256") and record.get("html_file"):
                family = record.get("observation", {}).get("family", "unknown")
                snapshot_rows.append((record["sha256"], str(manifest_path.parent / record["html_file"]), family))
        imported["snapshots"] += sync.record_snapshots(snapshot_rows)
        vacancies = [VacancyDTO.model_validate(row["parsed"]) for row in manifest.get("vacancies", []) if row.get("parsed")]
        if vacancies:
            query = str(manifest.get("query", "evidence-import"))
            source_url = str(manifest.get("search", {}).get("url", manifest_path.as_uri()))
            imported["vacancies"] += sync.sync_vacancies(f"evidence:{manifest_path.parent.name}", query, source_url, vacancies)["vacancies_seen"]
        threads = [ChatThreadDTO.model_validate(row["parsed"]) for row in manifest.get("threads", []) if row.get("parsed")]
        if threads:
            imported["threads"] += sync.sync_threads(threads)["threads_seen"]
    sync.project("phase3_evidence_import", imported)
    return imported
