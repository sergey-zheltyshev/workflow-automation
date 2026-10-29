from contextlib import closing
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, started_at TEXT, mode TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(name TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS vacancies(vacancy_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, company TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS chats(chat_id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS search_runs(id TEXT PRIMARY KEY, query TEXT NOT NULL, source_url TEXT NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS search_results(search_run_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, rank INTEGER NOT NULL, PRIMARY KEY(search_run_id, vacancy_id));
CREATE TABLE IF NOT EXISTS negotiations(vacancy_id TEXT PRIMARY KEY, status TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(chat_id TEXT NOT NULL, fingerprint TEXT NOT NULL, direction TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(chat_id, fingerprint));
CREATE TABLE IF NOT EXISTS chat_vacancy_links(chat_id TEXT PRIMARY KEY, vacancy_id TEXT, link_status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(sha256 TEXT PRIMARY KEY, path TEXT NOT NULL, family TEXT NOT NULL, captured_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_projection(key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS external_items(id TEXT PRIMARY KEY, vacancy_id TEXT, url TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS user_questions(id TEXT PRIMARY KEY, vacancy_id TEXT, question TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS action_intents(id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, target_json TEXT NOT NULL, text TEXT, text_sha256 TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS action_attempts(id INTEGER PRIMARY KEY, intent_id TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL, attempted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proofs(intent_id TEXT PRIMARY KEY, rule TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, verified_at TEXT NOT NULL);
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS vacancy_decisions(
    decision_id TEXT PRIMARY KEY,
    vacancy_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    source_run TEXT NOT NULL,
    decision TEXT NOT NULL,
    score INTEGER,
    reason TEXT NOT NULL,
    cover_letter TEXT,
    question_answers_json TEXT NOT NULL DEFAULT '{}',
    unknown_user_facts_json TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT NOT NULL,
    UNIQUE(vacancy_id, revision)
);
CREATE INDEX IF NOT EXISTS vacancy_decisions_actionable_idx
    ON vacancy_decisions(decision, vacancy_id, decided_at);
"""

SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS candidate_facts(
    fact_key TEXT PRIMARY KEY,
    fact_value TEXT NOT NULL,
    source TEXT NOT NULL,
    verified_at TEXT NOT NULL
);
"""

SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS candidate_profile_versions(
    version_id TEXT PRIMARY KEY, source_manifest_json TEXT NOT NULL, profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_profile_facts(
    version_id TEXT NOT NULL, fact_key TEXT NOT NULL, fact_value TEXT NOT NULL, source_ref TEXT NOT NULL,
    PRIMARY KEY(version_id, fact_key)
);
"""

SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS candidates(candidate_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_candidate ON candidates(active) WHERE active=1;
CREATE TABLE IF NOT EXISTS job_policy_versions(policy_version TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, policy_json TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id));
CREATE TABLE IF NOT EXISTS automation_policy_versions(policy_version TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, policy_json TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id));
CREATE TABLE IF NOT EXISTS account_bindings(binding_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, hh_identity TEXT NOT NULL, resume_id TEXT NOT NULL, resume_title TEXT NOT NULL, google_identities_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id));
CREATE TABLE IF NOT EXISTS run_contexts(run_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, candidate_profile_version TEXT NOT NULL, job_policy_version TEXT NOT NULL, automation_policy_version TEXT NOT NULL, binding_id TEXT NOT NULL, selected_resume_id TEXT NOT NULL, search_config_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_vacancy_state(candidate_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, eligibility TEXT NOT NULL, rule_id TEXT, detail TEXT NOT NULL, vacancy_hash TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(candidate_id,vacancy_id));
CREATE TABLE IF NOT EXISTS event_journal(event_id TEXT PRIMARY KEY, candidate_id TEXT, run_id TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
"""

# V9 is deliberately additive.  V1--V8 tables are forensic/legacy records and
# cannot safely acquire a candidate key in place (several have global primary
# keys).  All new production code must use these scoped tables instead.
SCHEMA_V9 = """
CREATE TABLE IF NOT EXISTS candidate_profiles(
    candidate_id TEXT NOT NULL, version_id TEXT NOT NULL, source_manifest_json TEXT NOT NULL,
    profile_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, version_id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE TABLE IF NOT EXISTS candidate_profile_facts_v2(
    candidate_id TEXT NOT NULL, version_id TEXT NOT NULL, fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL, source_ref TEXT NOT NULL,
    PRIMARY KEY(candidate_id, version_id, fact_key),
    FOREIGN KEY(candidate_id, version_id) REFERENCES candidate_profiles(candidate_id, version_id)
);
CREATE TABLE IF NOT EXISTS candidate_facts_v2(
    candidate_id TEXT NOT NULL, fact_key TEXT NOT NULL, fact_value TEXT NOT NULL,
    source TEXT NOT NULL, verified_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, fact_key), FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE TABLE IF NOT EXISTS candidate_vacancy_decisions(
    candidate_id TEXT NOT NULL, decision_id TEXT NOT NULL, vacancy_id TEXT NOT NULL,
    candidate_profile_version TEXT NOT NULL, job_policy_version TEXT NOT NULL,
    vacancy_revision TEXT NOT NULL, source_run TEXT NOT NULL, decision TEXT NOT NULL,
    score INTEGER, reason TEXT NOT NULL, cover_letter TEXT, question_answers_json TEXT NOT NULL,
    unknown_user_facts_json TEXT NOT NULL, decided_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, decision_id), UNIQUE(candidate_id, vacancy_id, vacancy_revision),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS candidate_decisions_actionable_idx
    ON candidate_vacancy_decisions(candidate_id, decision, vacancy_id, decided_at);
CREATE TABLE IF NOT EXISTS candidate_application_processes(
    candidate_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, binding_id TEXT NOT NULL,
    decision_id TEXT NOT NULL, resume_id TEXT NOT NULL, cover_sha256 TEXT NOT NULL,
    state TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, vacancy_id), FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE TABLE IF NOT EXISTS candidate_checkpoints(
    candidate_id TEXT NOT NULL, binding_id TEXT NOT NULL, name TEXT NOT NULL,
    value_json TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(candidate_id, binding_id, name)
);
CREATE TABLE IF NOT EXISTS candidate_projections(
    candidate_id TEXT NOT NULL, projection_key TEXT NOT NULL, value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY(candidate_id, projection_key)
);
"""

SCHEMA_V10 = """
CREATE TABLE IF NOT EXISTS candidate_chats(candidate_id TEXT NOT NULL, chat_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(candidate_id,chat_id));
CREATE TABLE IF NOT EXISTS candidate_messages(candidate_id TEXT NOT NULL, chat_id TEXT NOT NULL, fingerprint TEXT NOT NULL, direction TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY(candidate_id,chat_id,fingerprint));
CREATE TABLE IF NOT EXISTS candidate_chat_vacancy_links(candidate_id TEXT NOT NULL, chat_id TEXT NOT NULL, vacancy_id TEXT, link_status TEXT NOT NULL, detail TEXT NOT NULL, PRIMARY KEY(candidate_id,chat_id));
CREATE TABLE IF NOT EXISTS candidate_negotiations(candidate_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, status TEXT NOT NULL, source_url TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(candidate_id,vacancy_id));
CREATE TABLE IF NOT EXISTS candidate_external_items(candidate_id TEXT NOT NULL, id TEXT NOT NULL, vacancy_id TEXT, url TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(candidate_id,id));
"""

SCHEMA_V11 = """
CREATE TABLE IF NOT EXISTS candidate_runs(run_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, binding_id TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id));
"""

SCHEMA_V12 = """
CREATE TABLE IF NOT EXISTS vacancy_fact_revisions(
    vacancy_id TEXT NOT NULL, revision TEXT NOT NULL, facts_json TEXT NOT NULL,
    content_hash TEXT NOT NULL, source_run TEXT NOT NULL, captured_at TEXT NOT NULL,
    PRIMARY KEY(vacancy_id, revision)
);
"""

# V13 replaces the unscoped forensic event stream at the production boundary.
# Legacy tables stay immutable evidence; production reads/writes these tables.
SCHEMA_V13 = """
CREATE TABLE IF NOT EXISTS runtime_events(
    event_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, binding_id TEXT NOT NULL,
    run_id TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id),
    FOREIGN KEY(binding_id) REFERENCES account_bindings(binding_id)
);
CREATE INDEX IF NOT EXISTS runtime_events_scope_idx ON runtime_events(candidate_id,binding_id,run_id,created_at);
CREATE TABLE IF NOT EXISTS account_binding_observations(
    observation_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, binding_id TEXT NOT NULL,
    observed_hh_identity TEXT, observed_resume_id TEXT, observed_resume_url TEXT,
    observed_resume_title TEXT, identity_status TEXT NOT NULL, resume_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL, observed_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id),
    FOREIGN KEY(binding_id) REFERENCES account_bindings(binding_id)
);
CREATE INDEX IF NOT EXISTS binding_observations_latest_idx ON account_binding_observations(binding_id,observed_at DESC);
CREATE TABLE IF NOT EXISTS application_preflights(
    preflight_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, binding_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL, decision_id TEXT NOT NULL, resume_id TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL, evidence_json TEXT NOT NULL, observed_at TEXT NOT NULL,
    UNIQUE(candidate_id,vacancy_id,decision_id,preflight_id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id),
    FOREIGN KEY(binding_id) REFERENCES account_bindings(binding_id)
);
CREATE INDEX IF NOT EXISTS application_preflights_authorize_idx ON application_preflights(candidate_id,binding_id,vacancy_id,decision_id,status,observed_at DESC);
CREATE TABLE IF NOT EXISTS candidate_user_questions(
    candidate_id TEXT NOT NULL, id TEXT NOT NULL, vacancy_id TEXT, question TEXT NOT NULL,
    status TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(candidate_id,id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE TABLE IF NOT EXISTS candidate_search_runs(
    candidate_id TEXT NOT NULL, id TEXT NOT NULL, query TEXT NOT NULL, source_url TEXT NOT NULL,
    captured_at TEXT NOT NULL, PRIMARY KEY(candidate_id,id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE TABLE IF NOT EXISTS candidate_search_results(
    candidate_id TEXT NOT NULL, search_run_id TEXT NOT NULL, vacancy_id TEXT NOT NULL, rank INTEGER NOT NULL,
    PRIMARY KEY(candidate_id,search_run_id,vacancy_id),
    FOREIGN KEY(candidate_id,search_run_id) REFERENCES candidate_search_runs(candidate_id,id)
);
"""

# V14 is the durable hand-off boundary between the semantic operator and the
# program.  A work item is not an application plan: it records what still
# needs a human/LLM judgement and is always candidate scoped.
SCHEMA_V14 = """
CREATE TABLE IF NOT EXISTS candidate_work_items(
    candidate_id TEXT NOT NULL, item_id TEXT NOT NULL, run_id TEXT,
    kind TEXT NOT NULL, vacancy_id TEXT, state TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id,item_id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS candidate_work_items_pending_idx
    ON candidate_work_items(candidate_id,state,kind,updated_at);
"""

# V15 makes a vacancy revision independently reconstructable.  The existing
# facts JSON remains structured metadata; it is not repurposed as page text.
SCHEMA_V15_VIEW = """
CREATE VIEW IF NOT EXISTS candidate_vacancy_review AS
SELECT
    s.candidate_id,
    v.vacancy_id,
    v.title,
    v.company,
    v.url,
    f.revision AS latest_vacancy_revision,
    f.full_description,
    f.facts_json AS structured_facts_json,
    s.eligibility,
    s.rule_id AS eligibility_rule_id,
    s.detail AS eligibility_detail,
    d.decision,
    d.score,
    d.reason,
    d.cover_letter,
    d.vacancy_revision AS decision_vacancy_revision,
    d.source_run AS decision_source_run,
    f.source_run AS vacancy_source_run,
    f.captured_at,
    d.decided_at,
    p.state AS application_state,
    p.detail AS application_detail,
    p.updated_at AS application_updated_at
FROM candidate_vacancy_state s
JOIN vacancies v ON v.vacancy_id=s.vacancy_id
LEFT JOIN vacancy_fact_revisions f ON f.rowid=(
    SELECT f2.rowid FROM vacancy_fact_revisions f2
    WHERE f2.vacancy_id=v.vacancy_id
    ORDER BY f2.captured_at DESC, f2.rowid DESC LIMIT 1
)
LEFT JOIN candidate_vacancy_decisions d ON d.rowid=(
    SELECT d2.rowid FROM candidate_vacancy_decisions d2
    WHERE d2.candidate_id=s.candidate_id AND d2.vacancy_id=s.vacancy_id
    ORDER BY d2.decided_at DESC, d2.rowid DESC LIMIT 1
)
LEFT JOIN candidate_application_processes p
    ON p.candidate_id=s.candidate_id AND p.vacancy_id=s.vacancy_id;
"""

# V16 adds read-only operator projections.  These views deliberately surface
# missing business facts as NULL rather than inferring them from a process
# state, URL, or a generic runtime event.
SCHEMA_V16_VIEWS = """
CREATE VIEW IF NOT EXISTS application_review AS
SELECT
    p.candidate_id,
    p.vacancy_id,
    v.title,
    v.company,
    v.url,
    p.resume_id,
    d.decision_id,
    d.decision,
    d.score,
    d.reason AS fit_reason,
    d.cover_letter,
    d.question_answers_json AS employer_question_answers_json,
    p.state AS hh_confirmation_status,
    p.detail AS hh_confirmation_detail,
    p.updated_at AS application_updated_at,
    pf.status AS latest_preflight_status,
    pf.detail AS latest_preflight_detail,
    pf.evidence_json AS latest_preflight_evidence_json,
    pf.observed_at AS latest_preflight_observed_at,
    ev.kind AS latest_application_event_kind,
    ev.payload_json AS latest_application_event_evidence_json,
    ev.created_at AS latest_application_event_at
FROM candidate_application_processes p
JOIN vacancies v ON v.vacancy_id=p.vacancy_id
LEFT JOIN candidate_vacancy_decisions d
    ON d.candidate_id=p.candidate_id AND d.decision_id=p.decision_id
LEFT JOIN application_preflights pf ON pf.rowid=(
    SELECT pf2.rowid FROM application_preflights pf2
    WHERE pf2.candidate_id=p.candidate_id
      AND pf2.vacancy_id=p.vacancy_id
      AND pf2.decision_id=p.decision_id
    ORDER BY pf2.observed_at DESC, pf2.rowid DESC LIMIT 1
)
LEFT JOIN runtime_events ev ON ev.rowid=(
    SELECT ev2.rowid FROM runtime_events ev2
    WHERE ev2.candidate_id=p.candidate_id
      AND json_extract(ev2.payload_json, '$.vacancy_id')=p.vacancy_id
      AND ev2.kind IN (
          'application_in_flight', 'application_prepared',
          'application_reconciliation_required', 'application_reconciling',
          'application_reconciliation_completed', 'application_confirmed_payload_unknown',
          'application_confirmed_cover_pending', 'application_capability_blocked',
          'cover_attached'
      )
    ORDER BY ev2.created_at DESC, ev2.rowid DESC LIMIT 1
);

CREATE VIEW IF NOT EXISTS chat_review AS
SELECT
    c.candidate_id,
    c.chat_id,
    c.title AS employer,
    c.url AS chat_url,
    l.vacancy_id,
    v.title AS vacancy_title,
    v.company AS vacancy_company,
    l.link_status AS vacancy_link_status,
    l.detail AS vacancy_link_detail,
    m.direction AS last_message_direction,
    m.text AS last_message,
    NULL AS conversation_summary,
    json_extract(w.payload_json, '$.expected_text') AS suggested_answer,
    w.state AS reply_work_state,
    w.updated_at AS reply_work_updated_at,
    c.updated_at AS chat_updated_at
FROM candidate_chats c
LEFT JOIN candidate_chat_vacancy_links l
    ON l.candidate_id=c.candidate_id AND l.chat_id=c.chat_id
LEFT JOIN vacancies v ON v.vacancy_id=l.vacancy_id
LEFT JOIN candidate_messages m ON m.rowid=(
    SELECT m2.rowid FROM candidate_messages m2
    WHERE m2.candidate_id=c.candidate_id AND m2.chat_id=c.chat_id
    ORDER BY m2.rowid DESC LIMIT 1
)
LEFT JOIN candidate_work_items w ON w.rowid=(
    SELECT w2.rowid FROM candidate_work_items w2
    WHERE w2.candidate_id=c.candidate_id
      AND w2.kind='chat_reply'
      AND json_extract(w2.payload_json, '$.chat_id')=c.chat_id
    ORDER BY w2.updated_at DESC, w2.rowid DESC LIMIT 1
);
"""

# V17 is the operator boundary.  It stores reusable knowledge about HH screens
# separately from a particular browser run and projects one human-facing funnel
# row per vacancy.  A UI observation is never an action proof.
SCHEMA_V17 = """
CREATE TABLE IF NOT EXISTS hh_ui_contracts(
    surface TEXT NOT NULL,
    version TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(surface,version)
);
CREATE INDEX IF NOT EXISTS hh_ui_contracts_active_idx
    ON hh_ui_contracts(surface,active,observed_at DESC);

CREATE TABLE IF NOT EXISTS hh_ui_observations(
    observation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    url TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS hh_ui_observations_lookup_idx
    ON hh_ui_observations(candidate_id,surface,observed_at DESC);

CREATE TABLE IF NOT EXISTS hh_ui_incidents(
    incident_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    vacancy_id TEXT,
    surface TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    safe_resume_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS hh_ui_incidents_open_idx
    ON hh_ui_incidents(candidate_id,status,created_at DESC);
"""

SCHEMA_V17_VIEW = """
CREATE VIEW IF NOT EXISTS candidate_funnel AS
SELECT
    p.candidate_id,
    v.company AS company,
    v.title AS vacancy,
    CASE WHEN p.state LIKE 'APPLICATION_CONFIRMED%' OR p.state='application_confirmed_payload_unknown'
         THEN substr(p.updated_at, 1, 10) END AS application_date,
    NULL AS deadline,
    v.url AS vacancy_url,
    (SELECT c.url FROM candidate_chats c
       JOIN candidate_chat_vacancy_links l ON l.candidate_id=c.candidate_id AND l.chat_id=c.chat_id
      WHERE c.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id
      ORDER BY c.updated_at DESC LIMIT 1) AS chat_url,
    (SELECT e.url FROM candidate_external_items e
      WHERE e.candidate_id=p.candidate_id AND e.vacancy_id=p.vacancy_id AND e.kind='form'
      ORDER BY e.updated_at DESC LIMIT 1) AS form_url,
    (SELECT e.url FROM candidate_external_items e
      WHERE e.candidate_id=p.candidate_id AND e.vacancy_id=p.vacancy_id AND e.kind='test'
      ORDER BY e.updated_at DESC LIMIT 1) AS test_url,
    p.detail AS note,
    p.state AS status,
    p.updated_at AS last_live_check_at
FROM candidate_application_processes p
JOIN vacancies v ON v.vacancy_id=p.vacancy_id;
"""

# V18 separates an executable application queue from historical semantic
# decisions.  A decision is actionable only when it matches the active
# profile, policy and the latest full vacancy revision.  This is deliberately
# strict: a stale "apply" row must cause a fresh live review, never an apply.
SCHEMA_V18 = """
CREATE TABLE IF NOT EXISTS candidate_vacancy_lifecycle(
    candidate_id TEXT NOT NULL,
    vacancy_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'DISCOVERED','LIVE_READ','SEMANTIC_REVIEWED','READY_TO_APPLY',
        'FORM_REQUIRES_REVIEW','APPLICATION_IN_FLIGHT','APPLICATION_CONFIRMED',
        'COVER_PENDING','COVER_CONFIRMED','CHAT_REPLY_PENDING','CHAT_REPLY_CONFIRMED',
        'REJECTED','CLOSED','NEEDS_RECONCILIATION'
    )),
    decision_id TEXT,
    live_revision TEXT,
    deadline_at TEXT,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id,vacancy_id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
CREATE INDEX IF NOT EXISTS candidate_vacancy_lifecycle_state_idx
    ON candidate_vacancy_lifecycle(candidate_id,state,updated_at DESC);

CREATE VIEW IF NOT EXISTS candidate_action_queue AS
SELECT
    d.candidate_id,d.vacancy_id,d.decision_id,d.vacancy_revision,
    d.cover_letter,d.question_answers_json,v.url AS vacancy_url,d.decided_at
FROM candidate_vacancy_decisions d
JOIN candidate_vacancy_state s
  ON s.candidate_id=d.candidate_id AND s.vacancy_id=d.vacancy_id
JOIN vacancies v ON v.vacancy_id=d.vacancy_id
JOIN candidate_profiles cp
  ON cp.candidate_id=d.candidate_id AND cp.version_id=d.candidate_profile_version
JOIN job_policy_versions jp
  ON jp.candidate_id=d.candidate_id AND jp.policy_version=d.job_policy_version
JOIN candidate_decision_evidence de
  ON de.candidate_id=d.candidate_id AND de.decision_id=d.decision_id
JOIN vacancy_fact_revisions f
  ON f.vacancy_id=d.vacancy_id AND f.revision=d.vacancy_revision
LEFT JOIN candidate_application_processes p
  ON p.candidate_id=d.candidate_id AND p.vacancy_id=d.vacancy_id
LEFT JOIN candidate_vacancy_lifecycle l
  ON l.candidate_id=d.candidate_id AND l.vacancy_id=d.vacancy_id
WHERE d.decision='apply'
  AND s.eligibility='eligible'
  AND trim(COALESCE(d.cover_letter,''))<>''
  AND trim(COALESCE(f.full_description,''))<>''
  AND json_array_length(de.vacancy_evidence_json)>0
  AND json_array_length(de.candidate_fact_keys_json)>0
  AND d.candidate_profile_version=(
      SELECT cp2.version_id FROM candidate_profiles cp2
      WHERE cp2.candidate_id=d.candidate_id ORDER BY cp2.created_at DESC LIMIT 1
  )
  AND d.job_policy_version=(
      SELECT jp2.policy_version FROM job_policy_versions jp2
      WHERE jp2.candidate_id=d.candidate_id ORDER BY jp2.created_at DESC LIMIT 1
  )
  AND d.vacancy_revision=(
      SELECT f2.revision FROM vacancy_fact_revisions f2
      WHERE f2.vacancy_id=d.vacancy_id ORDER BY f2.captured_at DESC, f2.rowid DESC LIMIT 1
  )
  AND p.vacancy_id IS NULL
  AND (l.state IS NULL OR l.state='READY_TO_APPLY');

CREATE VIEW IF NOT EXISTS candidate_operator_funnel AS
SELECT
    p.candidate_id,
    v.company AS company,
    v.title AS vacancy,
    NULL AS application_date,
    l.deadline_at AS deadline,
    v.url AS vacancy_url,
    (SELECT c.url FROM candidate_chats c
       JOIN candidate_chat_vacancy_links cl ON cl.candidate_id=c.candidate_id AND cl.chat_id=c.chat_id
      WHERE c.candidate_id=p.candidate_id AND cl.vacancy_id=p.vacancy_id
      ORDER BY c.updated_at DESC LIMIT 1) AS chat_url,
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=p.candidate_id
       AND e.vacancy_id=p.vacancy_id AND e.kind='form' ORDER BY e.updated_at DESC LIMIT 1) AS form_url,
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=p.candidate_id
       AND e.vacancy_id=p.vacancy_id AND e.kind='test' ORDER BY e.updated_at DESC LIMIT 1) AS test_url,
    '[' || p.state || '] ' || p.detail AS note,
    p.updated_at AS last_live_check_at
FROM candidate_application_processes p
JOIN vacancies v ON v.vacancy_id=p.vacancy_id
LEFT JOIN candidate_vacancy_lifecycle l ON l.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id
UNION ALL
SELECT
    l.candidate_id,v.company,v.title,NULL,l.deadline_at,v.url,
    (SELECT c.url FROM candidate_chats c JOIN candidate_chat_vacancy_links cl
       ON cl.candidate_id=c.candidate_id AND cl.chat_id=c.chat_id
      WHERE c.candidate_id=l.candidate_id AND cl.vacancy_id=l.vacancy_id ORDER BY c.updated_at DESC LIMIT 1),
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=l.candidate_id
       AND e.vacancy_id=l.vacancy_id AND e.kind='form' ORDER BY e.updated_at DESC LIMIT 1),
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=l.candidate_id
       AND e.vacancy_id=l.vacancy_id AND e.kind='test' ORDER BY e.updated_at DESC LIMIT 1),
    '[' || l.state || '] ' || l.note,l.updated_at
FROM candidate_vacancy_lifecycle l
JOIN vacancies v ON v.vacancy_id=l.vacancy_id
WHERE NOT EXISTS(SELECT 1 FROM candidate_application_processes p
                 WHERE p.candidate_id=l.candidate_id AND p.vacancy_id=l.vacancy_id)
UNION ALL
SELECT
    e.candidate_id,v.company,v.title,NULL,NULL,v.url,NULL,
    CASE WHEN e.kind='form' THEN e.url END, CASE WHEN e.kind='test' THEN e.url END,
    '[EXTERNAL_' || e.kind || ':' || e.status || ']',e.updated_at
FROM candidate_external_items e
LEFT JOIN vacancies v ON v.vacancy_id=e.vacancy_id
WHERE e.vacancy_id IS NOT NULL
  AND NOT EXISTS(SELECT 1 FROM candidate_application_processes p
                 WHERE p.candidate_id=e.candidate_id AND p.vacancy_id=e.vacancy_id)
  AND NOT EXISTS(SELECT 1 FROM candidate_vacancy_lifecycle l
                 WHERE l.candidate_id=e.candidate_id AND l.vacancy_id=e.vacancy_id);
"""

# V19 makes semantic sales copy auditable.  New apply decisions must cite the
# exact live vacancy text and verified candidate fact keys; free-form prose
# alone cannot become an executable application.
SCHEMA_V19 = """
CREATE TABLE IF NOT EXISTS candidate_decision_evidence(
    candidate_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    vacancy_evidence_json TEXT NOT NULL,
    candidate_fact_keys_json TEXT NOT NULL,
    question_evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id,decision_id),
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);
"""

# V20 makes the operator funnel an actual chat/form SLA surface.  A deadline
# exists only when HH supplied a machine-readable message time or an explicit
# employer deadline; observation time is never converted into a fake SLA.
SCHEMA_V20_VIEW = """
DROP VIEW IF EXISTS candidate_operator_funnel;
CREATE VIEW candidate_operator_funnel AS
SELECT
    p.candidate_id, v.company AS company, v.title AS vacancy,
    CASE WHEN p.state LIKE 'APPLICATION_CONFIRMED%' THEN substr(p.updated_at,1,10) END AS application_date,
    COALESCE(l.deadline_at,(SELECT MIN(e.deadline_at) FROM candidate_external_items e WHERE e.candidate_id=p.candidate_id AND e.vacancy_id=p.vacancy_id AND e.deadline_at IS NOT NULL)) AS deadline,
    v.url AS vacancy_url,
    (SELECT c.url FROM candidate_chats c JOIN candidate_chat_vacancy_links cl ON cl.candidate_id=c.candidate_id AND cl.chat_id=c.chat_id WHERE c.candidate_id=p.candidate_id AND cl.vacancy_id=p.vacancy_id ORDER BY c.updated_at DESC LIMIT 1) AS chat_url,
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=p.candidate_id AND e.vacancy_id=p.vacancy_id AND e.kind='form' ORDER BY e.updated_at DESC LIMIT 1) AS form_url,
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=p.candidate_id AND e.vacancy_id=p.vacancy_id AND e.kind='test' ORDER BY e.updated_at DESC LIMIT 1) AS test_url,
    '[' || p.state || '] ' || p.detail AS note, p.updated_at AS last_live_check_at
FROM candidate_application_processes p JOIN vacancies v ON v.vacancy_id=p.vacancy_id
LEFT JOIN candidate_vacancy_lifecycle l ON l.candidate_id=p.candidate_id AND l.vacancy_id=p.vacancy_id
UNION ALL
SELECT l.candidate_id,v.company,v.title,NULL,COALESCE(l.deadline_at,(SELECT MIN(e.deadline_at) FROM candidate_external_items e WHERE e.candidate_id=l.candidate_id AND e.vacancy_id=l.vacancy_id AND e.deadline_at IS NOT NULL)),v.url,
    (SELECT c.url FROM candidate_chats c JOIN candidate_chat_vacancy_links cl ON cl.candidate_id=c.candidate_id AND cl.chat_id=c.chat_id WHERE c.candidate_id=l.candidate_id AND cl.vacancy_id=l.vacancy_id ORDER BY c.updated_at DESC LIMIT 1),
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=l.candidate_id AND e.vacancy_id=l.vacancy_id AND e.kind='form' ORDER BY e.updated_at DESC LIMIT 1),
    (SELECT e.url FROM candidate_external_items e WHERE e.candidate_id=l.candidate_id AND e.vacancy_id=l.vacancy_id AND e.kind='test' ORDER BY e.updated_at DESC LIMIT 1),
    '[' || l.state || '] ' || l.note,l.updated_at
FROM candidate_vacancy_lifecycle l JOIN vacancies v ON v.vacancy_id=l.vacancy_id
WHERE NOT EXISTS(SELECT 1 FROM candidate_application_processes p WHERE p.candidate_id=l.candidate_id AND p.vacancy_id=l.vacancy_id)
UNION ALL
SELECT e.candidate_id,v.company,v.title,NULL,e.deadline_at,v.url,NULL,
    CASE WHEN e.kind='form' THEN e.url END,CASE WHEN e.kind='test' THEN e.url END,
    '[EXTERNAL_' || e.kind || ':' || e.status || ']',e.updated_at
FROM candidate_external_items e LEFT JOIN vacancies v ON v.vacancy_id=e.vacancy_id
WHERE e.vacancy_id IS NOT NULL
  AND NOT EXISTS(SELECT 1 FROM candidate_application_processes p WHERE p.candidate_id=e.candidate_id AND p.vacancy_id=e.vacancy_id)
  AND NOT EXISTS(SELECT 1 FROM candidate_vacancy_lifecycle l WHERE l.candidate_id=e.candidate_id AND l.vacancy_id=e.vacancy_id);
"""


def backfill_vacancy_descriptions(database: Path, work_items_dir: Path) -> dict[str, object]:
    """Import full-text source observations without changing candidate state."""
    migrate(database)
    work_files = sorted(work_items_dir.glob("vacancies_*.jsonl"))
    counters = {
        "files": len(work_files), "rows": 0, "descriptions": 0,
        "revisions_updated": 0, "revisions_inserted": 0,
        "identities_inserted": 0, "unrecoverable": [],
    }
    with closing(sqlite3.connect(database)) as db, db:
        for path in work_files:
            source_run = path.stem.removeprefix("vacancies_")
            captured_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                counters["rows"] += 1
                row = json.loads(line)
                vacancy_id = str(row.get("vacancy_id") or "")
                full_description = str(row.get("description") or "").strip()
                if not vacancy_id.isdigit() or not full_description:
                    counters["unrecoverable"].append({"file": path.name, "line": line_number, "vacancy_id": vacancy_id, "reason": "missing_vacancy_id_or_description"})
                    continue
                counters["descriptions"] += 1
                identity = db.execute("SELECT 1 FROM vacancies WHERE vacancy_id=?", (vacancy_id,)).fetchone()
                if identity is None:
                    url, title, company = (str(row.get(key) or "").strip() for key in ("url", "title", "company"))
                    if not (url and title and company):
                        counters["unrecoverable"].append({"file": path.name, "line": line_number, "vacancy_id": vacancy_id, "reason": "missing_identity_fields"})
                        continue
                    db.execute(
                        "INSERT INTO vacancies(vacancy_id,url,title,company,updated_at) VALUES(?,?,?,?,?)",
                        (vacancy_id, url, title, company, captured_at),
                    )
                    counters["identities_inserted"] += 1
                existing = db.execute(
                    "SELECT full_description FROM vacancy_fact_revisions WHERE vacancy_id=? AND revision=?",
                    (vacancy_id, source_run),
                ).fetchone()
                if existing is not None:
                    if not str(existing[0] or "").strip():
                        db.execute(
                            "UPDATE vacancy_fact_revisions SET full_description=? WHERE vacancy_id=? AND revision=?",
                            (full_description, vacancy_id, source_run),
                        )
                        counters["revisions_updated"] += 1
                    continue
                facts = row.get("structured_facts")
                facts = facts if isinstance(facts, dict) else {}
                facts_json = json.dumps(facts, ensure_ascii=False, sort_keys=True)
                db.execute(
                    "INSERT INTO vacancy_fact_revisions(vacancy_id,revision,facts_json,content_hash,source_run,captured_at,full_description) VALUES(?,?,?,?,?,?,?)",
                    (vacancy_id, source_run, facts_json, hashlib.sha256(facts_json.encode()).hexdigest(), source_run, captured_at, full_description),
                )
                counters["revisions_inserted"] += 1
    return counters


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
        db.executescript(SCHEMA_V2)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")
        db.executescript(SCHEMA_V3)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
        db.executescript(SCHEMA_V4)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
        db.executescript(SCHEMA_V5)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")
        db.executescript(SCHEMA_V6)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")
        db.executescript(SCHEMA_V7)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)")
        db.executescript(SCHEMA_V8)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)")
        db.executescript(SCHEMA_V9)
        # One-way migration: legacy records belong only to the active Candidate A.
        # They remain preserved in legacy tables for audit; Candidate B starts empty.
        active = db.execute("SELECT candidate_id FROM candidates WHERE active=1").fetchone()
        if active:
            candidate_id = active[0]
            db.execute(
                "INSERT OR IGNORE INTO candidate_profiles(candidate_id,version_id,source_manifest_json,profile_json,created_at) "
                "SELECT ?,version_id,source_manifest_json,profile_json,created_at FROM candidate_profile_versions",
                (candidate_id,),
            )
            db.execute(
                "INSERT OR IGNORE INTO candidate_profile_facts_v2(candidate_id,version_id,fact_key,fact_value,source_ref) "
                "SELECT ?,version_id,fact_key,fact_value,source_ref FROM candidate_profile_facts",
                (candidate_id,),
            )
            db.execute(
                "INSERT OR IGNORE INTO candidate_facts_v2(candidate_id,fact_key,fact_value,source,verified_at) "
                "SELECT ?,fact_key,fact_value,source,verified_at FROM candidate_facts",
                (candidate_id,),
            )
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)")
        db.executescript(SCHEMA_V10)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (10)")
        db.executescript(SCHEMA_V11)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (11)")
        db.executescript(SCHEMA_V12)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (12)")
        db.executescript(SCHEMA_V13)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (13)")
        db.executescript(SCHEMA_V14)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (14)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(vacancy_fact_revisions)")}
        if "full_description" not in columns:
            db.execute("ALTER TABLE vacancy_fact_revisions ADD COLUMN full_description TEXT")
        db.executescript(SCHEMA_V15_VIEW)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (15)")
        db.executescript(SCHEMA_V16_VIEWS)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (16)")
        db.executescript(SCHEMA_V17)
        db.executescript(SCHEMA_V17_VIEW)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (17)")
        db.executescript(SCHEMA_V18)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (18)")
        db.executescript(SCHEMA_V19)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (19)")
        message_columns = {row[1] for row in db.execute("PRAGMA table_info(candidate_messages)")}
        if "received_at" not in message_columns:
            db.execute("ALTER TABLE candidate_messages ADD COLUMN received_at TEXT")
        external_columns = {row[1] for row in db.execute("PRAGMA table_info(candidate_external_items)")}
        for name in ("received_at", "deadline_at", "deadline_source"):
            if name not in external_columns:
                db.execute(f"ALTER TABLE candidate_external_items ADD COLUMN {name} TEXT")
        db.executescript(SCHEMA_V20_VIEW)
        db.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (20)")
        # `CREATE VIEW IF NOT EXISTS` cannot evolve an existing V18 view.
        # Rebuild only the executable queue so V19 evidence is a hard runtime
        # gate for already-migrated production databases too.
        db.execute("DROP VIEW IF EXISTS candidate_action_queue")
        db.executescript(SCHEMA_V18)
