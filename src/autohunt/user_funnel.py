"""Human-readable vacancy funnel projected from authoritative SQLite."""

from __future__ import annotations

from contextlib import closing

import json
import os
import sqlite3
import subprocess
from pathlib import Path

from .context import bootstrap
from .operator_state import FUNNEL_COLUMNS as OPERATOR_FUNNEL_COLUMNS
from .operator_state import funnel_rows

# XLSX is an operator hand-off, not an internal scoring dump.  Keep exactly
# the business columns also returned by `autohunt funnel`.
FUNNEL_COLUMNS = list(OPERATOR_FUNNEL_COLUMNS)


def _human_gate(value: str, detail: str) -> str:
    if value == "hard_reject" and detail == "salary_floor":
        return "Оплата по вакансии целиком ниже минимально допустимой для кандидата."
    if value == "hard_reject":
        return "Вакансия не проходит одно из обязательных условий кандидата."
    if value == "unknown_requires_semantic":
        return "Нужна индивидуальная оценка полного текста вакансии."
    return "Базовые условия вакансии подходят; окончательное решение зависит от задач и требований."


def _stage(process: str | None, negotiation: str | None, incoming: str | None, outgoing: str | None, has_question: bool, has_interview: bool) -> str:
    if has_interview:
        return "Интервью"
    if has_question:
        return "Нужно решение"
    if negotiation == "closed_refusal":
        return "Отказ"
    if process and process.startswith("application_confirmed") or process == "confirmed":
        return "Отклик отправлен"
    if negotiation and "interview" in negotiation:
        return "Интервью"
    if incoming:
        return "AutoHunt ответил" if outgoing else "Работодатель написал"
    if process in {"prepared", "application_confirmed_cover_pending"}:
        return "Не откликались"
    return "Найдена"


def _matches(title: str, facts: dict[str, object], score: int | None) -> str:
    values: list[str] = []
    lowered = title.lower()
    if any(token in lowered for token in ("ассистент", "assistant", "chief of staff")):
        values.append("Опыт бизнес-ассистента: координация, статусы, документы и контроль задач.")
    if facts.get("work_format") == "remote":
        values.append("Удалённый формат соответствует предпочтениям кандидата.")
    if score and score >= 75:
        values.append("Семантическая оценка подтвердила сильное совпадение с задачами роли.")
    return " ".join(values) or "Индивидуальные совпадения ещё не описаны — вакансия ждёт оценки."


def _risks(gate: str, detail: str, decision: str | None) -> str:
    if gate == "hard_reject":
        return _human_gate(gate, detail)
    if decision is None:
        return "Полный текст ещё не оценен по задачам, требованиям и качеству будущего письма."
    return "Существенных рисков после индивидуальной оценки не зафиксировано."


def build_vacancy_funnel(database: Path) -> list[dict[str, object]]:
    """One human row per candidate vacancy; never becomes a source of truth."""
    return funnel_rows(database)


def _legacy_build_vacancy_funnel(database: Path) -> list[dict[str, object]]:
    """Deprecated internal diagnostic projection; never exported to Sergey."""
    context = bootstrap(database)
    candidate_id = str(context.get("candidate_id") or "")
    if not candidate_id:
        return []
    with closing(sqlite3.connect(database)) as db, db:
        rows = db.execute(
            "SELECT s.vacancy_id,v.company,v.title,v.url,s.eligibility,s.detail,"
            "d.score,d.reason,d.decision,d.cover_letter,d.unknown_user_facts_json,d.decided_at,"
            "p.state,p.detail,p.updated_at,n.status,"
            "(SELECT c.url FROM candidate_chats c JOIN candidate_chat_vacancy_links l ON l.candidate_id=c.candidate_id AND l.chat_id=c.chat_id WHERE l.candidate_id=s.candidate_id AND l.vacancy_id=s.vacancy_id LIMIT 1),"
            "(SELECT m.text FROM candidate_messages m JOIN candidate_chat_vacancy_links l ON l.candidate_id=m.candidate_id AND l.chat_id=m.chat_id WHERE l.candidate_id=s.candidate_id AND l.vacancy_id=s.vacancy_id AND m.direction='incoming' ORDER BY m.rowid DESC LIMIT 1),"
            "(SELECT m.text FROM candidate_messages m JOIN candidate_chat_vacancy_links l ON l.candidate_id=m.candidate_id AND l.chat_id=m.chat_id WHERE l.candidate_id=s.candidate_id AND l.vacancy_id=s.vacancy_id AND m.direction='outgoing' ORDER BY m.rowid DESC LIMIT 1),"
            "(SELECT url FROM candidate_external_items e WHERE e.candidate_id=s.candidate_id AND e.vacancy_id=s.vacancy_id AND e.kind='form' LIMIT 1),"
            "(SELECT url FROM candidate_external_items e WHERE e.candidate_id=s.candidate_id AND e.vacancy_id=s.vacancy_id AND e.kind='test' LIMIT 1),"
            "(SELECT question FROM candidate_user_questions q WHERE q.candidate_id=s.candidate_id AND q.vacancy_id=s.vacancy_id AND q.status='open' ORDER BY q.created_at DESC LIMIT 1),"
            "EXISTS(SELECT 1 FROM candidate_work_items w WHERE w.candidate_id=s.candidate_id AND w.vacancy_id=s.vacancy_id AND w.kind='interview' AND w.state='pending'),"
            "f.facts_json "
            "FROM candidate_vacancy_state s JOIN vacancies v ON v.vacancy_id=s.vacancy_id "
            "LEFT JOIN candidate_vacancy_decisions d ON d.candidate_id=s.candidate_id AND d.vacancy_id=s.vacancy_id "
            "LEFT JOIN candidate_application_processes p ON p.candidate_id=s.candidate_id AND p.vacancy_id=s.vacancy_id "
            "LEFT JOIN candidate_negotiations n ON n.candidate_id=s.candidate_id AND n.vacancy_id=s.vacancy_id "
            "LEFT JOIN vacancy_fact_revisions f ON f.vacancy_id=s.vacancy_id AND f.captured_at=(SELECT MAX(f2.captured_at) FROM vacancy_fact_revisions f2 WHERE f2.vacancy_id=s.vacancy_id) "
            "WHERE s.candidate_id=? ORDER BY COALESCE(p.updated_at,d.decided_at,s.updated_at) DESC",
            (candidate_id,),
        ).fetchall()
    result: list[dict[str, object]] = []
    for row in rows:
        (
            vacancy_id, company, title, url, gate, detail, score, reason, decision, cover, _unknown_facts,
            _decided_at, process, _process_detail, process_updated, negotiation, chat_url, incoming, outgoing,
            form_url, test_url, user_question, has_interview, raw_facts,
        ) = row
        facts = json.loads(raw_facts) if raw_facts else {}
        decision_reason = str(reason or _human_gate(str(gate), str(detail)))
        not_applied = ""
        if not process or not str(process).startswith("application_confirmed"):
            not_applied = decision_reason if decision != "apply" else "Сопроводительное подготовлено; отклик ещё не подтверждён HH."
        note = ""
        if process == "application_confirmed_payload_unknown":
            note = "HH подтверждает отклик, но не раскрывает отправленные resume/cover в доступном UI."
        if user_question:
            note = f"Нужен ответ: {user_question}"
        effective_score = int(score) if score is not None else (15 if gate == "hard_reject" else 55)
        result.append({
            "Компания": company or "Не указана",
            "Вакансия": title or f"Вакансия {vacancy_id}",
            "Оценка 0–100": effective_score,
            "Почему такая оценка": decision_reason if score is not None else "Предварительная оценка: " + _human_gate(str(gate), str(detail)),
            "Сильные совпадения": _matches(str(title or ""), facts, score),
            "Риски / слабые места": _risks(str(gate), str(detail), decision),
            "Почему не откликались": not_applied,
            "Стадия": _stage(process, negotiation, incoming, outgoing, bool(user_question), bool(has_interview)),
            "Дата отклика": process_updated if process and str(process).startswith("application_confirmed") else None,
            "Сопроводительное": cover or "",
            "Последнее сообщение работодателя": incoming or "",
            "Последний ответ AutoHunt": outgoing or "",
            "Что нужно от пользователя": str(user_question or ""),
            "Дедлайн": None,
            "Ссылка на вакансию": url or "",
            "Ссылка на чат": chat_url or "",
            "Ссылка на внешнюю анкету": form_url or "",
            "Ссылка на тест": test_url or "",
            "_note": note,
        })
    return result


def export_funnel_xlsx(root: Path, run_id: str) -> Path:
    """Create a styled immutable XLSX snapshot with the bundled artifact tool."""
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    payload_path = reports / f".AUTOHUNT_RUN_{run_id}.json"
    output_path = reports / f"AUTOHUNT_RUN_{run_id}.xlsx"
    payload_path.write_text(json.dumps({"run_id": run_id, "columns": FUNNEL_COLUMNS, "rows": build_vacancy_funnel(root / "var" / "autohunt.sqlite")}, ensure_ascii=False), encoding="utf-8")
    node = os.environ.get("AUTOHUNT_NODE", "node")
    script = root / "scripts" / "export_funnel_xlsx.mjs"
    try:
        subprocess.run([node, str(script), str(payload_path), str(output_path)], check=True, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError("XLSX export requires configured AUTOHUNT_NODE runtime") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"XLSX export failed: {exc.stderr.strip()}") from exc
    finally:
        payload_path.unlink(missing_ok=True)
    return output_path


def summarize_business_run(rows: list[dict[str, object]], *, chat_triage: dict[str, int], reports: list[dict[str, object]], replies_sent: int) -> dict[str, int]:
    """Compact human counts for CLI/Codex; no technical lifecycle labels."""
    metrics = [dict(report.get("metrics") or {}) for report in reports]
    return {
        "chats_checked": sum(int(metric.get("chat_urls", 0)) for metric in metrics),
        "chat_events": sum(int(metric.get("chats_changed", 0)) for metric in metrics),
        "replies_sent": replies_sent,
        "interviews": int(chat_triage.get("interviews", 0)),
        "needs_user": sum(1 for row in rows if "Нужен" in str(row.get("Примечание") or "")),
        "queries": sum(1 for metric in metrics if metric.get("query") and not metric.get("chat_only")),
        "vacancies_read": sum(int(metric.get("ready_confirmed", 0)) for metric in metrics),
        "applications_confirmed": sum(1 for row in rows if row.get("Дата отклика")),
        "employer_messages": 0,
        "found": len(rows),
    }
