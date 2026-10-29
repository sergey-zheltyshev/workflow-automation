import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .business_batch import BusinessBatch
from .candidate_facts import all_facts, record_fact
from .chat import (
    execute_direct_authoritative_chat_replies,
    execute_direct_authoritative_cover_attachment,
)
from .config import Settings
from .context import bootstrap, list_candidates, select_candidate
from .decisions import import_decisions, status
from .direct_batch import execute_direct_authoritative_batch
from .direct_search import collect_direct_full_live
from .live_adapter import run_browser_acceptance
from .live_run import LiveReadOnlyRun, write_benchmark
from .operator_state import funnel_rows
from .operator_state import summary as operator_summary
from .production_browser import project_profile
from .storage import migrate
from .ui_memory import map_status, seed_static_contracts
from .workflow import WorkBatchControl, WorkCoordinator


def main() -> None:
    p = argparse.ArgumentParser(prog="autohunt")
    p.add_argument("command", choices=["doctor", "migrate", "run", "work", "import-decisions", "status", "record-fact", "apply-batch", "apply-direct", "batch-apply-direct", "search-direct", "batch-read-direct", "reply-direct", "cover-direct", "bootstrap", "candidate", "browser-acceptance", "funnel", "ui-map-status", "batch-status", "batch-start-read"])
    p.add_argument("work_action", nargs="?", choices=["start", "next", "commit", "finish"])
    p.add_argument("--db", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--query", default="операционный ассистент")
    p.add_argument("--queries-file", type=Path)
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--max-full", type=int, default=2500)
    p.add_argument("--max-workers", type=int, default=8)
    p.add_argument("--chat-only", action="store_true")
    p.add_argument("--skip-chat-sync", action="store_true")
    p.add_argument("--cdp-endpoint", default=None, help="legacy read-only route; never production")
    p.add_argument("--plan", type=Path)
    p.add_argument("--decisions", type=Path)
    p.add_argument("--fact-key")
    p.add_argument("--fact-value")
    p.add_argument("--live", action="store_true")
    p.add_argument("--candidate-id")
    p.add_argument("--vacancy-id")
    p.add_argument("--chat-id")
    p.add_argument("--run-id")
    p.add_argument("--source-run")
    p.add_argument("--output", type=Path)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--run-label", choices=["run1", "run2"])
    p.add_argument("--target-url", default="https://hh.ru/applicant/resumes")
    p.add_argument("--wait-for-auth-seconds", type=int, default=0)
    args = p.parse_args()
    project_root = Path.cwd()
    db = Path(args.db) if args.db else Settings.from_project_root(project_root).database_path
    if args.command == "browser-acceptance":
        if not args.run_label:
            p.error("browser-acceptance requires --run-label run1 or run2")
        command_line = os.environ.get("AUTOHUNT_EXECUTION_COMMAND", " ".join([sys.executable, *sys.argv]))
        evidence = asyncio.run(
            run_browser_acceptance(
                project_root,
                run_label=args.run_label,
                target_url=args.target_url,
                execution_command=command_line,
                wait_for_auth_seconds=args.wait_for_auth_seconds,
            )
        )
        print(json.dumps(evidence, ensure_ascii=False))
        if evidence["exception_type"] is not None:
            raise SystemExit(1)
    elif args.command == "migrate":
        migrate(db)
        print(f"migrated {db}")
    elif args.command == "doctor":
        migrate(db)
        print(json.dumps({
            "database": "ready" if db.exists() else "missing",
            "production_browser_profile": str(project_profile(project_root)),
            "production_browser_route": "direct persistent Playwright / Chrome Stable",
            "legacy_cdp_route": "disabled unless explicit --cdp-endpoint is supplied for read-only research",
            "operator_summary": operator_summary(db),
        }, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps({**status(db), "candidate_facts": len(all_facts(db))}, ensure_ascii=False))
    elif args.command == "bootstrap":
        print(json.dumps(bootstrap(db), ensure_ascii=False))
    elif args.command == "funnel":
        print(json.dumps({"columns": ["Компания", "Вакансия", "Дата отклика", "Дедлайн", "Полный URL вакансии", "Полный URL чата", "Полный URL анкеты", "Полный URL тестового", "Примечание"], "rows": funnel_rows(db)}, ensure_ascii=False))
    elif args.command == "batch-status":
        print(json.dumps(BusinessBatch(db).status(), ensure_ascii=False))
    elif args.command == "batch-start-read":
        queries = [value.strip() for value in args.query.split("||") if value.strip()]
        print(json.dumps({"run_id": BusinessBatch(db).start_read(queries=queries), "next": "run direct read phase, then complete_read"}, ensure_ascii=False))
    elif args.command == "batch-read-direct":
        queries = [value.strip() for value in args.query.split("||") if value.strip()]
        batch = BusinessBatch(db)
        run_id = batch.start_read(queries=queries)
        try:
            result = asyncio.run(collect_direct_full_live(project_root, queries=queries, max_pages=args.max_pages, max_cards_per_query=args.max_full))
            batch.complete_read(run_id, full_read=int(result["full_read"]), incidents=len(result["incidents"]))
        except Exception as exc:
            batch.fail_read(run_id, detail=f"{type(exc).__name__}: {exc}")
            raise
        print(json.dumps({"batch_run_id": run_id, **result, "next": "semantic review in ChatGPT Desktop; then batch-apply-direct --live --run-id"}, ensure_ascii=False))
    elif args.command == "apply-direct":
        if not args.live:
            p.error("apply-direct is irreversible; pass --live after reviewing bootstrap and funnel")
        print(json.dumps(asyncio.run(execute_direct_authoritative_batch(project_root, limit=args.limit)).__dict__, ensure_ascii=False))
    elif args.command == "batch-apply-direct":
        if not args.live or not args.run_id:
            p.error("batch-apply-direct is irreversible; requires --live and --run-id")
        batch = BusinessBatch(db)
        batch.begin_apply(args.run_id)
        report = asyncio.run(execute_direct_authoritative_batch(project_root, limit=args.limit))
        batch.complete_apply(args.run_id, applied=report.applied, stopped=report.stopped)
        print(json.dumps({"batch_run_id": args.run_id, **report.__dict__}, ensure_ascii=False))
    elif args.command == "search-direct":
        queries = [value.strip() for value in args.query.split("||") if value.strip()]
        print(json.dumps(asyncio.run(collect_direct_full_live(project_root, queries=queries, max_pages=args.max_pages, max_cards_per_query=args.max_full)), ensure_ascii=False))
    elif args.command == "reply-direct":
        if not args.live:
            p.error("reply-direct is irreversible; pass --live after reviewing prepared chat rows")
        print(json.dumps(asyncio.run(execute_direct_authoritative_chat_replies(project_root, limit=args.limit, chat_id=args.chat_id)), ensure_ascii=False))
    elif args.command == "cover-direct":
        if not args.live or not args.vacancy_id:
            p.error("cover-direct is irreversible; requires --live and --vacancy-id")
        print(json.dumps({"outcome": asyncio.run(execute_direct_authoritative_cover_attachment(project_root, vacancy_id=args.vacancy_id))}, ensure_ascii=False))
    elif args.command == "ui-map-status":
        seed_static_contracts(db)
        print(json.dumps(map_status(db), ensure_ascii=False))
    elif args.command == "candidate":
        if args.candidate_id:
            select_candidate(db, args.candidate_id)
            print(json.dumps(bootstrap(db), ensure_ascii=False))
        else:
            print(json.dumps(list_candidates(db), ensure_ascii=False))
    elif args.command == "record-fact":
        if not args.fact_key or not args.fact_value:
            p.error("record-fact requires --fact-key and --fact-value")
        record_fact(db, args.fact_key, args.fact_value, source="user-confirmed Codex chat")
        print(json.dumps({"recorded": args.fact_key}, ensure_ascii=False))
    elif args.command == "import-decisions":
        if args.decisions is None:
            p.error("import-decisions requires --decisions")
        print(json.dumps({"imported": import_decisions(db, args.decisions)}, ensure_ascii=False))
    elif args.command == "run":
        if not args.dry_run:
            p.error("live run is read-only; pass --dry-run")
        if not args.cdp_endpoint:
            p.error("legacy CDP reader requires explicit --cdp-endpoint; it is not the production browser route")
        if args.max_pages < 1 or args.max_full < 1 or not 1 <= args.max_workers <= 10:
            p.error("--max-pages/max-full must be positive; --max-workers must be 1..10")
        root = Path.cwd()
        queries = json.loads(args.queries_file.read_text(encoding="utf-8")) if args.queries_file else [args.query]
        if not isinstance(queries, list) or not all(isinstance(value, str) for value in queries):
            p.error("--queries-file must contain a JSON string array")
        report = asyncio.run(
            LiveReadOnlyRun(
                root, cdp_endpoint=args.cdp_endpoint, query=args.query, max_pages=args.max_pages,
                max_full=args.max_full, max_workers=args.max_workers, chat_only=args.chat_only,
                skip_chat_sync=args.skip_chat_sync, queries=queries,
            ).run()
        )
        benchmark = write_benchmark(root, report)
        print(json.dumps(report, ensure_ascii=False))
        print(f"benchmark={benchmark}")
    elif args.command == "work":
        if args.work_action:
            control = WorkBatchControl(db)
            if args.work_action == "start":
                result = control.start()
            elif args.work_action == "next":
                if not args.run_id:
                    p.error("work next requires --run-id")
                result = control.next(args.run_id, limit=args.limit, source_run=args.source_run)
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    result = {"run_id": result["run_id"], "status": result["status"], "source_run": result.get("source_run"), "vacancies": len(result["vacancies"]), "output": str(args.output)}
            elif args.work_action == "commit":
                if not args.run_id or args.decisions is None:
                    p.error("work commit requires --run-id and --decisions")
                result = control.commit(args.run_id, args.decisions)
            else:
                if not args.run_id:
                    p.error("work finish requires --run-id")
                result = control.finish(args.run_id, project_root)
            print(json.dumps(result, ensure_ascii=False))
            return
        if not args.dry_run:
            p.error("work read-only collection requires --dry-run; live execution is only reached by the official coordinator after durable semantic decisions and preflight")
        print(json.dumps(asyncio.run(WorkCoordinator(project_root, cdp_endpoint=args.cdp_endpoint).run(query_hint=args.query)), ensure_ascii=False))
    else:
        p.error("apply-batch is disabled pending candidate-scoped queue and mandatory production preflight")


if __name__ == "__main__":
    main()
