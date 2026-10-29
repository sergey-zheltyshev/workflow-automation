"""Official restart-safe `работай` coordinator."""

from contextlib import closing
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .application_run import (
    execute_authoritative_application_queue,
    preflight_authoritative_application_queue,
    verify_live_account_binding,
)
from .chat import (
    execute_authoritative_chat_replies,
    prepare_known_chat_replies,
    triage_changed_chats,
)
from .context import bootstrap, evaluate_persisted_vacancy
from .decisions import VacancyDecisionInput, create_semantic_work_item, persist_semantic_decisions
from .live_run import LiveReadOnlyRun
from .protocol import WorkItem
from .storage import migrate
from .sync import RegistrySync
from .user_funnel import build_vacancy_funnel, export_funnel_xlsx, summarize_business_run


class MvpWorkflow:
    def __init__(self, database: Path, output_dir: Path) -> None:
        migrate(database)
        self.database = database
        self.output_dir = output_dir

    def run(self, run_id: str, minutes: int = 15, *, captcha_detected: bool = False) -> list[WorkItem]:
        if minutes < 1:
            raise ValueError("minutes must be positive")
        now = datetime.now(UTC)
        with closing(sqlite3.connect(self.database)) as db, db:
            active = db.execute("SELECT c.candidate_id,b.binding_id FROM candidates c JOIN account_bindings b ON b.candidate_id=c.candidate_id AND b.active=1 WHERE c.active=1").fetchone()
            if active is None:
                raise RuntimeError("active candidate/account binding required")
            candidate_id, binding_id = active
            db.execute("INSERT OR IGNORE INTO runs(id,started_at,mode,status) VALUES(?,?,?,?)", (run_id, now.isoformat(), "mvp", "running"))
            db.execute("INSERT OR REPLACE INTO candidate_runs(run_id,candidate_id,binding_id,mode,status,started_at) VALUES(?,?,?,?,?,?)", (run_id, candidate_id, binding_id, "mvp", "running", now.isoformat()))
            if captcha_detected:
                db.execute("UPDATE runs SET status='stopped_captcha' WHERE id=?", (run_id,))
                db.execute("INSERT INTO checkpoints(name,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", ("mvp_run", json.dumps({"run_id": run_id, "status": "stopped_captcha"}), now.isoformat()))
                db.execute("INSERT INTO candidate_checkpoints(candidate_id,binding_id,name,value_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,binding_id,name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (candidate_id, binding_id, "mvp_run", json.dumps({"run_id":run_id,"status":"stopped_captcha"}), now.isoformat()))
                return []
            unlinked = db.execute("SELECT chat_id FROM candidate_chat_vacancy_links WHERE candidate_id=? AND link_status='error' ORDER BY chat_id", (candidate_id,)).fetchall()
            linked = db.execute("SELECT chat_id,vacancy_id FROM candidate_chat_vacancy_links WHERE candidate_id=? AND link_status='linked' ORDER BY chat_id", (candidate_id,)).fetchall()
            vacancies = db.execute("SELECT vacancy_id FROM vacancies ORDER BY updated_at DESC LIMIT 10").fetchall()
            items = [WorkItem(id=f"chat-{chat_id}", kind="chat_review", chat_id=chat_id, evidence_refs=[f"chat:{chat_id}"], created_at=now) for (chat_id,) in unlinked]
            items += [WorkItem(id=f"chat-{chat_id}", kind="chat_review", chat_id=chat_id, vacancy_id=vacancy_id, evidence_refs=[f"chat:{chat_id}", f"vacancy:{vacancy_id}"], created_at=now) for chat_id, vacancy_id in linked]
            items += [WorkItem(id=f"vacancy-{vacancy_id}", kind="vacancy_review", vacancy_id=vacancy_id, evidence_refs=[f"vacancy:{vacancy_id}"], created_at=now) for (vacancy_id,) in vacancies]
            payload = [item.model_dump(mode="json") for item in items]
            db.execute("INSERT INTO checkpoints(name,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", ("mvp_run", json.dumps({"run_id": run_id, "minutes": minutes, "items": len(items)}), now.isoformat()))
            db.execute("INSERT INTO candidate_checkpoints(candidate_id,binding_id,name,value_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,binding_id,name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (candidate_id, binding_id, "mvp_run", json.dumps({"run_id":run_id,"minutes":minutes,"items":len(items)}), now.isoformat()))
            db.execute("UPDATE runs SET status='checkpointed' WHERE id=?", (run_id,))
            db.execute("UPDATE candidate_runs SET status='checkpointed',finished_at=? WHERE run_id=?", (now.isoformat(), run_id))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "mvp_user_table.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return items


class WorkBatchControl:
    """Official durable hand-off: program batches facts, Codex resolves them."""

    def __init__(self, database: Path) -> None:
        migrate(database)
        self.database = database

    def start(self) -> dict[str, object]:
        context = bootstrap(self.database)
        candidate_id = str(context.get("candidate_id") or "")
        binding = dict(context.get("account_binding") or {})
        if not candidate_id or not binding:
            raise RuntimeError("active candidate/account binding required")
        with closing(sqlite3.connect(self.database)) as db, db:
            existing = db.execute(
                "SELECT run_id FROM candidate_runs WHERE candidate_id=? AND binding_id=? AND mode='semantic_batch' AND status='running' ORDER BY started_at DESC LIMIT 1",
                (candidate_id, binding["binding_id"]),
            ).fetchone()
            if existing:
                return {"run_id": str(existing[0]), "resumed": True}
            run_id = f"semantic-batch-{uuid.uuid4()}"
            db.execute(
                "INSERT INTO candidate_runs(run_id,candidate_id,binding_id,mode,status,started_at) VALUES(?,?,?,?,?,?)",
                (run_id, candidate_id, binding["binding_id"], "semantic_batch", "running", datetime.now(UTC).isoformat()),
            )
        return {"run_id": run_id, "resumed": False}

    def next(self, run_id: str, *, limit: int = 20, source_run: str | None = None) -> dict[str, object]:
        if not 1 <= limit <= 30:
            raise ValueError("batch limit must be 1..30")
        context = bootstrap(self.database)
        candidate_id = str(context.get("candidate_id") or "")
        with closing(sqlite3.connect(self.database)) as db, db:
            run = db.execute("SELECT 1 FROM candidate_runs WHERE run_id=? AND candidate_id=? AND mode='semantic_batch' AND status='running'", (run_id, candidate_id)).fetchone()
            if not run:
                raise RuntimeError("current semantic batch run is not running")
            profile = db.execute("SELECT profile_json FROM candidate_profiles WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            policy = db.execute("SELECT policy_json FROM job_policy_versions WHERE candidate_id=? ORDER BY created_at DESC LIMIT 1", (candidate_id,)).fetchone()
            source_filter = " AND json_extract(w.payload_json, '$.source_run')=?" if source_run else ""
            rows = db.execute(
                "SELECT w.item_id,w.vacancy_id,w.payload_json,v.title,v.company,v.url,s.eligibility,s.detail,"
                "f.revision,f.facts_json,f.full_description,f.source_run FROM candidate_work_items w "
                "JOIN vacancies v ON v.vacancy_id=w.vacancy_id "
                "LEFT JOIN candidate_vacancy_state s ON s.candidate_id=w.candidate_id AND s.vacancy_id=w.vacancy_id "
                "LEFT JOIN vacancy_fact_revisions f ON f.rowid=COALESCE("
                "(SELECT f1.rowid FROM vacancy_fact_revisions f1 WHERE f1.vacancy_id=w.vacancy_id AND f1.revision=json_extract(w.payload_json,'$.source_run') LIMIT 1),"
                "(SELECT f2.rowid FROM vacancy_fact_revisions f2 WHERE f2.vacancy_id=w.vacancy_id ORDER BY f2.captured_at DESC,f2.rowid DESC LIMIT 1)) "
                f"WHERE w.candidate_id=? AND w.kind='semantic_decision' AND w.state='pending'{source_filter} ORDER BY w.updated_at DESC LIMIT ?",
                (candidate_id, source_run, limit) if source_run else (candidate_id, limit),
            ).fetchall()
            for item_id, *_ in rows:
                db.execute("UPDATE candidate_work_items SET run_id=?,updated_at=? WHERE candidate_id=? AND item_id=? AND state='pending'", (run_id, datetime.now(UTC).isoformat(), candidate_id, item_id))
        if not rows:
            return {"run_id": run_id, "status": "done", "source_run": source_run, "vacancies": [], "chats": []}
        vacancies = []
        for _item_id, vacancy_id, raw_payload, title, company, url, eligibility, detail, revision, raw_facts, full_description, row_source_run in rows:
            facts = json.loads(raw_facts) if raw_facts else {}
            vacancies.append({
                "vacancy_id": vacancy_id, "title": title, "company": company, "url": url,
                "structured_facts": facts, "full_description": str(full_description or ""),
                "deterministic_eligibility": eligibility, "deterministic_reason": detail,
                "revision": revision, "source_run": row_source_run, "source_payload": json.loads(raw_payload),
            })
        return {"run_id": run_id, "status": "pending", "source_run": source_run, "candidate_profile": json.loads(profile[0]) if profile else {}, "job_policy": json.loads(policy[0]) if policy else {}, "vacancies": vacancies, "chats": []}

    def commit(self, run_id: str, path: Path) -> dict[str, int]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_rows = payload.get("decisions", payload) if isinstance(payload, dict) else payload
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("decision file must contain a nonempty JSON decisions array")
        context = bootstrap(self.database)
        candidate_id = str(context.get("candidate_id") or "")
        with closing(sqlite3.connect(self.database)) as db, db:
            allowed = {
                str(vacancy_id): str(revision)
                for vacancy_id, revision in db.execute(
                    "SELECT w.vacancy_id,f.revision FROM candidate_work_items w "
                    "JOIN vacancy_fact_revisions f ON f.vacancy_id=w.vacancy_id AND f.captured_at=(SELECT MAX(f2.captured_at) FROM vacancy_fact_revisions f2 WHERE f2.vacancy_id=w.vacancy_id) "
                    "WHERE w.candidate_id=? AND w.run_id=? AND w.kind='semantic_decision' AND w.state='pending'",
                    (candidate_id, run_id),
                )
            }
        models: list[VacancyDecisionInput] = []
        for raw in raw_rows:
            vacancy_id = str(raw.get("vacancy_id") or "")
            if vacancy_id not in allowed:
                raise ValueError(f"vacancy was not issued to current run: {vacancy_id}")
            value = dict(raw)
            value["decision"] = "needs_review" if value.get("decision") == "needs_user" else value.get("decision")
            value["revision"] = allowed[vacancy_id]
            value["source_run"] = run_id
            models.append(VacancyDecisionInput.model_validate(value))
        accepted = persist_semantic_decisions(self.database, models)
        counts = {"accepted": accepted, "apply": 0, "skip": 0, "needs_user": 0, "rejected": 0}
        for item in models:
            counts["needs_user" if item.decision == "needs_review" else item.decision] += 1
        return counts

    def finish(self, run_id: str, root: Path) -> dict[str, object]:
        context = bootstrap(self.database)
        candidate_id = str(context.get("candidate_id") or "")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE candidate_runs SET status='checkpointed',finished_at=? WHERE run_id=? AND candidate_id=? AND mode='semantic_batch' AND status='running'", (datetime.now(UTC).isoformat(), run_id, candidate_id))
        registry = RegistrySync(self.database)
        registry.project("semantic_batch", {"run_id": run_id, "status": "checkpointed"})
        return {"run_id": run_id, "status": "checkpointed", "funnel_xlsx": str(export_funnel_xlsx(root, run_id))}


class WorkCoordinator:
    """Official resumable `работай` path. It never accepts an application plan."""
    def __init__(self, root: Path, *, cdp_endpoint: str, minutes: int = 15) -> None:
        self.root, self.database, self.cdp_endpoint, self.minutes = root, root / "var" / "autohunt.sqlite", cdp_endpoint, minutes

    def _queries(self, context: dict[str, object], completed: list[dict[str, object]], hint: str | None) -> list[str]:
        """Choose later searches from actual prior outcomes, not CLI fixed strings."""
        policy = dict(context.get("job_policy") or {})
        configured = [str(query).strip() for query in policy.get("search_queries", []) if str(query).strip()]
        if not hint and configured:
            # Candidate policy owns business direction.  Do not let generic
            # historical role names pull marketplace/personal-assistant noise
            # ahead of IT/digital/product vacancies.
            return configured[:5]
        roles = [str(role) for role in policy.get("target_roles", [])]
        initial = hint or (roles[0] if roles else "бизнес ассистент")
        queries = [f"{initial} удаленно"]
        seen_titles = " ".join(str(r.get("metrics", {}).get("query", "")) for r in completed).lower()
        # The first round's actual deterministic outcomes feed the next query;
        # card count alone never makes a query look successful.
        with closing(sqlite3.connect(self.database)) as db, db:
            outcomes = db.execute("SELECT eligibility,detail FROM candidate_vacancy_state WHERE candidate_id=? ORDER BY updated_at DESC LIMIT 30", (context["candidate_id"],)).fetchall()
        rejected_rules = {str(row[1]) for row in outcomes if row[0] == "hard_reject"}
        if any("assistant" in role.lower() for role in roles) and "операцион" not in seen_titles:
            queries.append("операционный ассистент удаленно")
        if any("coordinator" in role.lower() for role in roles) and "координатор" not in seen_titles and "salary_floor" not in rejected_rules:
            queries.append("проектный координатор удаленно")
        elif "salary_floor" in rejected_rules:
            queries.append("операционный ассистент удаленно зарплата")
        return queries[:3]

    def _checkpoint(self, context: dict[str, object], run_id: str, stage: str, payload: dict[str, object]) -> None:
        stamp = datetime.now(UTC).isoformat()
        value = json.dumps({"run_id": run_id, "stage": stage, **payload}, ensure_ascii=False)
        binding = dict(context["account_binding"])
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO candidate_checkpoints(candidate_id,binding_id,name,value_json,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,binding_id,name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (context["candidate_id"], binding["binding_id"], "work", value, stamp))
            db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"work:{run_id}:{stage}:{uuid.uuid4()}", context["candidate_id"], binding["binding_id"], run_id, f"work_{stage}", value, stamp))

    async def run(
        self,
        *,
        query_hint: str | None = None,
        semantic_resolver: Callable[[dict[str, object]], VacancyDecisionInput | None] | None = None,
        allow_mutation: bool = False,
    ) -> dict[str, object]:
        if self.minutes < 1:
            raise ValueError("minutes must be positive")
        started, deadline = time.monotonic(), time.monotonic() + self.minutes * 60
        context = bootstrap(self.database)
        required = ("candidate_id", "candidate_profile_version", "job_policy_version", "automation_policy_version", "account_binding")
        if any(not context.get(key) for key in required):
            raise RuntimeError("bootstrap requires active candidate/profile/policies/account binding")
        binding = dict(context["account_binding"])
        run_id = f"work-{uuid.uuid4()}"
        with closing(sqlite3.connect(self.database)) as db, db:
            # A prior process can be killed by OS/UI without executing its
            # finally block.  Never let its durable `running` row masquerade
            # as an active collector after a fresh official invocation.
            stale = db.execute("SELECT run_id FROM candidate_runs WHERE candidate_id=? AND binding_id=? AND status='running'", (context["candidate_id"], binding["binding_id"])).fetchall()
            for (stale_run,) in stale:
                db.execute("UPDATE candidate_runs SET status='interrupted_before_new_work',finished_at=? WHERE run_id=?", (datetime.now(UTC).isoformat(), stale_run))
                db.execute("INSERT INTO runtime_events(event_id,candidate_id,binding_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (f"work-recovered:{stale_run}:{uuid.uuid4()}", context["candidate_id"], binding["binding_id"], stale_run, "run_recovered_after_interruption", json.dumps({"replacement_run": run_id}, ensure_ascii=False), datetime.now(UTC).isoformat()))
            db.execute("INSERT INTO run_contexts(run_id,candidate_id,candidate_profile_version,job_policy_version,automation_policy_version,binding_id,selected_resume_id,search_config_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (run_id, context["candidate_id"], context["candidate_profile_version"], context["job_policy_version"], context["automation_policy_version"], binding["binding_id"], binding["resume_id"], json.dumps({"deadline_seconds": self.minutes * 60}, ensure_ascii=False), datetime.now(UTC).isoformat()))
            db.execute("INSERT INTO candidate_runs(run_id,candidate_id,binding_id,mode,status,started_at) VALUES(?,?,?,?,?,?)", (run_id, context["candidate_id"], binding["binding_id"], "work", "running", datetime.now(UTC).isoformat()))
        reports: list[dict[str, object]] = []
        self._checkpoint(context, run_id, "started", {"remaining_seconds": self.minutes * 60})
        identity = await verify_live_account_binding(self.root, cdp_endpoint=self.cdp_endpoint)
        if identity["identity_status"] != "MATCH" or identity["resume_status"] != "MATCH":
            self._checkpoint(context, run_id, "identity_blocked", {"identity": identity})
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE candidate_runs SET status='stopped_identity',finished_at=? WHERE run_id=?", (datetime.now(UTC).isoformat(), run_id))
            return {"run_id": run_id, "candidate_id": context["candidate_id"], "stopped": "live_account_or_resume_not_match", "identity": identity}
        self._checkpoint(context, run_id, "identity_matched", {"identity": identity})
        # Exactly one chat phase; every later collector suppresses chat sync.
        first = LiveReadOnlyRun(self.root, cdp_endpoint=self.cdp_endpoint, query=query_hint or "бизнес ассистент", max_pages=1, max_full=1, max_workers=8, chat_only=True)
        reports.append(await first.run())
        chat_rows = (Path(reports[-1].get("chat_work_items", "")).read_text(encoding="utf-8").splitlines() if reports[-1].get("chat_work_items") else [])
        chat_ids = [str(json.loads(row)["chat_id"]) for row in chat_rows]
        chat_triage = triage_changed_chats(self.database, chat_ids, run_id=run_id)
        prepared_replies = prepare_known_chat_replies(self.database, chat_ids)
        replies_sent: list[str] = []
        if allow_mutation and prepared_replies and time.monotonic() < deadline:
            replies_sent = await execute_authoritative_chat_replies(self.root, cdp_endpoint=self.cdp_endpoint, limit=prepared_replies)
        self._checkpoint(context, run_id, "chats_complete", {"chat_report": reports[-1], "chat_triage": chat_triage, "prepared_replies": prepared_replies, "replies_sent": replies_sent, "remaining_seconds": max(0, int(deadline - time.monotonic()))})
        semantic_items: list[str] = []
        for _round in range(3):
            candidates = self._queries(context, reports, query_hint)
            seen = {str(report.get("metrics", {}).get("query", "")) for report in reports}
            query = next((item for item in candidates if item not in seen), None)
            if query is None:
                break
            if time.monotonic() >= deadline:
                break
            report = await LiveReadOnlyRun(self.root, cdp_endpoint=self.cdp_endpoint, query=query, max_pages=1, max_full=10, max_workers=8, skip_chat_sync=True).run()
            reports.append(report)
            for row in (Path(report.get("work_items", "")).read_text(encoding="utf-8").splitlines() if report.get("work_items") else []):
                item=json.loads(row)
                evaluate_persisted_vacancy(self.database, item["vacancy_id"], report["metrics"]["run_id"])
                work_id = create_semantic_work_item(self.database, vacancy_id=item["vacancy_id"], source_run=run_id)
                if work_id:
                    semantic_items.append(work_id)
            self._checkpoint(context, run_id, "search_round", {"query": query, "collector_run": report["metrics"]["run_id"], "remaining_seconds": max(0, int(deadline - time.monotonic()))})
        resolved = 0
        if semantic_resolver:
            with closing(sqlite3.connect(self.database)) as db, db:
                tasks = db.execute("SELECT vacancy_id,payload_json FROM candidate_work_items WHERE candidate_id=? AND kind='semantic_decision' AND state='pending'", (context["candidate_id"],)).fetchall()
            decisions = [decision for vacancy_id, payload in tasks if (decision := semantic_resolver({"vacancy_id": vacancy_id, **json.loads(payload)})) is not None]
            if decisions:
                resolved = persist_semantic_decisions(self.database, decisions)
        queue = []
        preflights: list[dict[str, object]] = []
        application_report = None
        if resolved and time.monotonic() < deadline:
            from .application_run import build_authoritative_application_queue
            queue = build_authoritative_application_queue(self.database)
            preflights = await preflight_authoritative_application_queue(self.root, cdp_endpoint=self.cdp_endpoint)
            if allow_mutation and any(row["preflight"] == "clear" for row in preflights):
                application_report = await execute_authoritative_application_queue(self.root, cdp_endpoint=self.cdp_endpoint, limit=1)
        summary={"run_id": run_id, "candidate_id":context["candidate_id"],"deadline_seconds":self.minutes*60,"remaining_seconds":max(0, int(deadline-time.monotonic())),"elapsed_seconds":round(time.monotonic()-started,2),"reports":reports,"chat_triage":chat_triage,"prepared_replies":prepared_replies,"replies_sent":replies_sent,"pending_semantic_items":semantic_items,"semantic_resolved":resolved,"authoritative_queue":len(queue),"preflights":preflights,"application":application_report.__dict__ if application_report else None,"next":"checkpointed"}
        self._checkpoint(context, run_id, "semantic_pending", summary)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE candidate_runs SET status='checkpointed',finished_at=? WHERE run_id=?", (datetime.now(UTC).isoformat(), run_id))
        registry = RegistrySync(self.database)
        registry.project("current_workflow", summary)
        summary["registry_rows"] = len(registry.rebuild_registry_projection())
        summary["funnel_xlsx"] = str(export_funnel_xlsx(self.root, run_id))
        summary["business_summary"] = summarize_business_run(build_vacancy_funnel(self.database), chat_triage=chat_triage, reports=reports, replies_sent=len(replies_sent))
        return summary
