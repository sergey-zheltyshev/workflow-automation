"""Candidate-agnostic authoritative context and deterministic vacancy eligibility."""
from __future__ import annotations

from contextlib import closing

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .operator_state import summary as operator_summary
from .storage import migrate


def now() -> str:
    return datetime.now(UTC).isoformat()


def list_candidates(database: Path) -> list[dict[str, object]]:
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        return [{"candidate_id": row[0], "display_name": row[1], "active": bool(row[2])} for row in db.execute("SELECT candidate_id,display_name,active FROM candidates ORDER BY display_name")]


def select_candidate(database: Path, candidate_id: str) -> None:
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        if db.execute("SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone() is None:
            raise KeyError(f"unknown candidate: {candidate_id}")
        db.execute("UPDATE candidates SET active=0")
        db.execute("UPDATE candidates SET active=1 WHERE candidate_id=?", (candidate_id,))


def provision_candidate(
    database: Path, *, candidate_id: str, display_name: str, job_policy: dict[str, object],
    automation_policy: dict[str, object], account_binding: dict[str, object],
    job_policy_version: str, automation_policy_version: str, binding_id: str,
) -> None:
    """Create isolated candidate configuration. No person-specific code or shared facts."""
    migrate(database)
    required = {"hh_identity", "resume_id", "resume_title"}
    if not candidate_id or not display_name or required - set(account_binding):
        raise ValueError("candidate and stable account binding are required")
    stamp = now()
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT OR IGNORE INTO candidates(candidate_id,display_name,created_at) VALUES(?,?,?)", (candidate_id, display_name, stamp))
        db.execute("INSERT OR REPLACE INTO job_policy_versions VALUES(?,?,?,?,?)", (job_policy_version, candidate_id, json.dumps(job_policy, ensure_ascii=False), "bootstrap", stamp))
        db.execute("INSERT OR REPLACE INTO automation_policy_versions VALUES(?,?,?,?,?)", (automation_policy_version, candidate_id, json.dumps(automation_policy, ensure_ascii=False), "bootstrap", stamp))
        db.execute("UPDATE account_bindings SET active=0 WHERE candidate_id=?", (candidate_id,))
        db.execute("INSERT OR REPLACE INTO account_bindings VALUES(?,?,?,?,?,?,?,?)", (binding_id, candidate_id, account_binding["hh_identity"], account_binding["resume_id"], account_binding["resume_title"], json.dumps(account_binding.get("google_identities", []), ensure_ascii=False), 1, stamp))

def bootstrap(database: Path) -> dict[str, object]:
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        row=db.execute("SELECT candidate_id,display_name FROM candidates WHERE active=1").fetchone()
        if not row: return {"next_allowed_operation":"candidate_setup_required"}
        cid, name = row
        binding = db.execute("SELECT binding_id,hh_identity,resume_id,resume_title,google_identities_json FROM account_bindings WHERE candidate_id=? AND active=1", (cid,)).fetchone()
        profile = db.execute("SELECT version_id,profile_json FROM candidate_profiles WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
        policy = db.execute("SELECT policy_version,policy_json FROM job_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
        automation = db.execute("SELECT policy_version,policy_json FROM automation_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (cid,)).fetchone()
        binding_data = {"binding_id": binding[0], "hh_identity": binding[1], "resume_id": binding[2], "resume_title": binding[3], "google_identities": json.loads(binding[4])} if binding else None
        pending = db.execute(
            "SELECT kind,vacancy_id,item_id FROM candidate_work_items WHERE candidate_id=? AND state='pending' ORDER BY updated_at LIMIT 20",
            (cid,),
        ).fetchall()
        processes = db.execute(
            "SELECT vacancy_id,state FROM candidate_application_processes WHERE candidate_id=? AND state IN ('in_flight','needs_reconciliation','application_confirmed_cover_pending') ORDER BY updated_at",
            (cid,),
        ).fetchall()
        questions = db.execute("SELECT id,vacancy_id,question FROM candidate_user_questions WHERE candidate_id=? AND status='open' ORDER BY created_at", (cid,)).fetchall()
        checkpoint = db.execute("SELECT value_json FROM candidate_checkpoints WHERE candidate_id=? AND binding_id=? AND name='work'", (cid, binding[0] if binding else "")).fetchone()
        next_operation = "reconcile_application" if processes else "resolve_user_question" if questions else "resolve_semantic" if pending else "read_only_run"
        return {"candidate_id": cid, "candidate": name, "candidate_profile_version": profile[0] if profile else None, "candidate_profile": json.loads(profile[1]) if profile else {}, "job_policy_version": policy[0] if policy else None, "automation_policy_version": automation[0] if automation else None, "job_policy": json.loads(policy[1]) if policy else {}, "automation_policy": json.loads(automation[1]) if automation else {}, "account_binding": binding_data, "pending_work": [{"kind": x[0], "vacancy_id": x[1], "item_id": x[2]} for x in pending], "application_reconciliation": [{"vacancy_id": x[0], "state": x[1]} for x in processes], "open_user_questions": [{"id": x[0], "vacancy_id": x[1], "question": x[2]} for x in questions], "work_checkpoint": json.loads(checkpoint[0]) if checkpoint else None, "operator_summary": operator_summary(database), "required_agent_files": ["AGENT_START.md", "PROJECT_STATE.md", "SOURCE_INDEX.md"], "next_allowed_operation": next_operation}


def migrate_live_binding_placeholder(database: Path) -> None:
    """One-way Candidate A migration; requires fresh selected card + HH account proof."""
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        binding = db.execute("SELECT b.binding_id,b.candidate_id,b.hh_identity,b.resume_id,b.resume_title FROM account_bindings b JOIN candidates c ON c.candidate_id=b.candidate_id WHERE c.active=1 AND b.active=1").fetchone()
        if not binding or binding[2] != "HH identity requires live verification" or not str(binding[3]).startswith("resume:"):
            raise RuntimeError("binding is not an unmigrated placeholder")
        observation = db.execute("SELECT observed_hh_identity,observed_resume_id,observed_resume_title FROM account_binding_observations WHERE candidate_id=? AND binding_id=? AND observed_hh_identity IS NOT NULL AND observed_resume_id IS NOT NULL ORDER BY observed_at DESC LIMIT 1", (binding[1], binding[0])).fetchone()
        if not observation or str(binding[4]).lower() not in str(observation[2] or "").lower() or not str(observation[1]).startswith("https://hh.ru/resume/"):
            raise RuntimeError("fresh live identity and selected resume card are required")
        db.execute("UPDATE account_bindings SET hh_identity=?,resume_id=? WHERE binding_id=?", (observation[0], observation[1], binding[0]))
        db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"binding-migration:{binding[0]}", binding[1], binding[0], None, "account_binding_migrated_from_live_observation", json.dumps({"resume_id": observation[1]}, ensure_ascii=False), now()))


def enable_application_automation_after_acceptance(database: Path) -> str:
    """Versioned transition; Gateway still requires this exact enabled policy."""
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute("SELECT candidate_id,policy_version,policy_json FROM automation_policy_versions WHERE candidate_id=(SELECT candidate_id FROM candidates WHERE active=1) ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("active automation policy missing")
        policy = json.loads(row[2])
        if policy.get("hh_applications") == "enabled":
            return str(row[1])
        if policy.get("hh_applications") != "disabled_pending_read_only_acceptance":
            raise RuntimeError("automation policy is not eligible for acceptance transition")
        version = f"{row[1]}-accepted"
        policy["hh_applications"] = "enabled"
        db.execute("INSERT INTO automation_policy_versions(policy_version,candidate_id,policy_json,source,created_at) VALUES(?,?,?,?,?)", (version, row[0], json.dumps(policy, ensure_ascii=False), "user-authorized live acceptance", now()))
        binding = db.execute("SELECT binding_id FROM account_bindings WHERE candidate_id=? AND active=1", (row[0],)).fetchone()
        if binding:
            db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"automation-acceptance:{version}", row[0], binding[0], None, "automation_policy_enabled_after_acceptance", json.dumps({"policy_version": version}, ensure_ascii=False), now()))
        return version


def enable_confirmed_fact_chat_replies(database: Path) -> str:
    """User-authorized operational setting: only fact-backed HH replies."""
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute("SELECT candidate_id,policy_version,policy_json FROM automation_policy_versions WHERE candidate_id=(SELECT candidate_id FROM candidates WHERE active=1) ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            raise RuntimeError("active automation policy missing")
        policy = json.loads(row[2])
        if policy.get("hh_replies") == "enabled":
            return str(row[1])
        policy["hh_replies"] = "enabled"
        policy["hh_reply"] = "enabled"
        version = f"{row[1]}-chat-replies"
        db.execute("INSERT INTO automation_policy_versions(policy_version,candidate_id,policy_json,source,created_at) VALUES(?,?,?,?,?)", (version, row[0], json.dumps(policy, ensure_ascii=False), "user-authorized business chat replies", now()))
        return version

def eligibility(policy: dict[str, object], vacancy: dict[str, object]) -> tuple[str,str|None]:
    provenance = dict(vacancy.get("provenance") or {})
    def explicit(key: str) -> bool:
        return provenance.get(key, "explicit_hh_metadata") in {"explicit_hh_metadata", "explicit_vacancy_text"}
    # Search-card metadata and generic policy heuristics may prioritise work,
    # but may not suppress a full-text review.  Sergey allows one automatic
    # rejection only: a vacancy explicitly states onsite-only work.
    work_format = vacancy.get("work_format")
    if work_format == "onsite" and explicit("work_format"):
        return "hard_reject", "onsite_only"
    salary=vacancy.get("salary_from"); ceiling=vacancy.get("salary_to"); floor=policy.get("salary_floor")
    if floor is not None and salary is not None and explicit("salary_from") and vacancy.get("currency") == policy.get("currency"):
        # A range crossing floor is not proven below the floor.
        if (ceiling if ceiling is not None else salary) < floor: return "unknown_requires_semantic","salary_floor"
    elif floor is not None and salary is not None and vacancy.get("currency") not in (None, policy.get("currency")):
        return "unknown_requires_semantic","salary_foreign"
    elif floor is not None and salary is None and not policy.get("allow_missing_salary", True):
        return "unknown_requires_semantic","salary_unknown"
    schedule = vacancy.get("schedule")
    if schedule is not None and explicit("schedule") and schedule in set(policy.get("forbidden_schedules",[])): return "unknown_requires_semantic","schedule"
    if work_format is not None and explicit("work_format") and work_format not in set(policy.get("allowed_work_formats",[])): return "unknown_requires_semantic","work_format"
    country = vacancy.get("country")
    # Employer country is not applicant geography for a remote role unless policy says it is.
    if country is not None and explicit("country") and country not in set(policy.get("allowed_countries",[])) and work_format in {"onsite", "hybrid"}: return "unknown_requires_semantic","geography"
    if schedule is None and policy.get("required_schedules"): return "unknown_requires_semantic","schedule_unknown"
    if country is None and work_format in {"onsite", "hybrid"} and policy.get("require_location_for_nonremote", True): return "unknown_requires_semantic","geography_unknown"
    if work_format is None: return "unknown_requires_semantic","work_format_unknown"
    return "eligible",None


def evaluate_and_record_eligibility(database: Path, vacancy: dict[str, object], *, candidate_id: str | None = None) -> tuple[str, str | None]:
    """Persist deterministic policy result before semantic work or any mutation path."""
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        if candidate_id is None:
            row = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
            if row is None:
                raise RuntimeError("active candidate is required")
            candidate_id = str(row[0])
        policy_row = db.execute(
            "SELECT policy_json FROM job_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if policy_row is None:
            raise RuntimeError("job policy is required")
        vacancy_id = str(vacancy.get("vacancy_id") or "")
        if not vacancy_id.isdigit():
            raise ValueError("vacancy_id is required")
        result, rule = eligibility(json.loads(policy_row[0]), vacancy)
        detail = rule or "policy_pass"
        fingerprint = json.dumps(vacancy, sort_keys=True, ensure_ascii=False)
        db.execute(
            "INSERT INTO candidate_vacancy_state(candidate_id,vacancy_id,eligibility,rule_id,detail,vacancy_hash,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(candidate_id,vacancy_id) DO UPDATE SET eligibility=excluded.eligibility,rule_id=excluded.rule_id,detail=excluded.detail,vacancy_hash=excluded.vacancy_hash,updated_at=excluded.updated_at",
            (candidate_id, vacancy_id, result, rule, detail, fingerprint, now()),
        )
        return result, rule


def evaluate_persisted_vacancy(database: Path, vacancy_id: str, revision: str, *, candidate_id: str | None = None) -> tuple[str, str | None]:
    """Gate entrypoint for production: facts must come from authoritative collected revision."""
    migrate(database)
    with closing(sqlite3.connect(database)) as db, db:
        row = db.execute("SELECT facts_json FROM vacancy_fact_revisions WHERE vacancy_id=? AND revision=?", (vacancy_id, revision)).fetchone()
    if row is None:
        raise RuntimeError(f"structured vacancy facts missing: {vacancy_id}@{revision}")
    facts = json.loads(row[0])
    facts["vacancy_id"] = vacancy_id
    return evaluate_and_record_eligibility(database, facts, candidate_id=candidate_id)
