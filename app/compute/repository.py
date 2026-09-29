from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


class ComputeRepository:
    """封装计算任务运营领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def template_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE code=?", (code,)).fetchone()

    def template_by_id(self, template_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_templates WHERE id=?", (template_id,)).fetchone()

    def active_templates(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM compute_templates WHERE active=1 ORDER BY code,version").fetchall()
        return [dict(row) for row in rows]

    def create_template(self, *, code: str, name: str, algorithm: str, parameter_schema: dict[str, Any], defaults: dict[str, Any], max_runtime_seconds: int, max_attempts: int, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_templates(code,name,algorithm,version,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES(?,?,?,1,?,?,?,?,1,?,?,?)",
            (code, name, algorithm, json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), json.dumps(defaults, ensure_ascii=False, sort_keys=True), max_runtime_seconds, max_attempts, created_by, now, now),
        )
        return dict(self.template_by_id(cursor.lastrowid))

    # ---- 模板版本 ----

    def version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_template_versions WHERE id=?", (version_id,)).fetchone()

    def version_by_number(self, template_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_template_versions WHERE template_id=? AND version=?", (template_id, version)).fetchone()

    def versions_for_template(self, template_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM compute_template_versions WHERE template_id=? ORDER BY version", (template_id,)).fetchall()

    def effective_version(self, template_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_template_versions WHERE template_id=? AND status='published' AND effective_at<=? ORDER BY effective_at DESC, version DESC LIMIT 1",
            (template_id, now),
        ).fetchone()

    def latest_published_version(self, template_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_template_versions WHERE template_id=? AND status='published' ORDER BY version DESC LIMIT 1", (template_id,)).fetchone()

    def next_version_number(self, template_id: int) -> int:
        return int(self.connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM compute_template_versions WHERE template_id=?", (template_id,)).fetchone()[0])

    def insert_version(self, *, template_id: int, version: int, status: str, name: str, algorithm: str, parameter_schema: dict[str, Any], default_parameters: dict[str, Any], max_runtime_seconds: int, max_attempts: int, base_version: int | None, effective_at: str | None, published_by: str | None, published_at: str | None, digest: str, actor: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO compute_template_versions(template_id,version,status,name,algorithm,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,base_version,effective_at,published_by,published_at,content_digest,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                template_id, version, status, name, algorithm,
                json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), json.dumps(default_parameters, ensure_ascii=False, sort_keys=True),
                max_runtime_seconds, max_attempts, base_version, effective_at, published_by, published_at, digest, actor, now, now,
            ),
        )
        return self.version_by_id(cursor.lastrowid)

    def update_draft_content(self, version_id: int, *, name: str, algorithm: str, parameter_schema: dict[str, Any], default_parameters: dict[str, Any], max_runtime_seconds: int, max_attempts: int, digest: str, now: str) -> None:
        self.connection.execute(
            "UPDATE compute_template_versions SET name=?,algorithm=?,parameter_schema_json=?,default_parameters_json=?,max_runtime_seconds=?,max_attempts=?,content_digest=?,updated_at=? WHERE id=? AND status='draft'",
            (
                name, algorithm, json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True), json.dumps(default_parameters, ensure_ascii=False, sort_keys=True),
                max_runtime_seconds, max_attempts, digest, now, version_id,
            ),
        )

    def mark_version_published(self, version_id: int, *, effective_at: str, published_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE compute_template_versions SET status='published',effective_at=?,published_by=?,published_at=?,updated_at=? WHERE id=? AND status='draft'",
            (effective_at, published_by, now, now, version_id),
        )
        return cursor.rowcount

    def delete_version(self, version_id: int) -> None:
        self.connection.execute("DELETE FROM compute_template_versions WHERE id=?", (version_id,))

    def count_version_tasks(self, version_id: int) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE template_version_id=?", (version_id,)).fetchone()[0])

    def quota(self, subject_type: str, subject_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_quotas WHERE subject_type=? AND subject_key=?", (subject_type, subject_key)).fetchone()

    def upsert_quota(self, *, subject_type: str, subject_key: str, max_queued: int, max_running: int, daily_submissions: int, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO compute_quotas(subject_type,subject_key,max_queued,max_running,daily_submissions,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subject_type,subject_key) DO UPDATE SET max_queued=excluded.max_queued,max_running=excluded.max_running,daily_submissions=excluded.daily_submissions,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (subject_type, subject_key, max_queued, max_running, daily_submissions, actor, now, now),
        )
        return dict(self.quota(subject_type, subject_key))

    def count_user_states(self, requested_by: str) -> dict[str, int]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks WHERE requested_by=? GROUP BY status", (requested_by,)).fetchall()
        return {str(row["status"]): int(row["amount"]) for row in rows}

    def count_user_submissions_since(self, requested_by: str, since: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM compute_tasks WHERE requested_by=? AND created_at>=?", (requested_by, since)).fetchone()[0])

    def task_by_id(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT t.*,tpl.code AS template_code,COALESCE(tv.algorithm, tpl.algorithm) AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id LEFT JOIN compute_template_versions tv ON tv.id=t.template_version_id WHERE t.id=?",
            (task_id,),
        ).fetchone()

    def task_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_tasks WHERE requested_by=? AND idempotency_key=?", (requested_by, key)).fetchone()

    def create_task(self, *, template_id: int, template_version_id: int, template_version: int, validation_snapshot: dict[str, Any], project_code: str, requested_by: str, parameters: dict[str, Any], parameter_digest: str, priority: int, idempotency_key: str, max_attempts: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_id,template_version_id,template_version,validation_snapshot_json,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'queued',0,?,?,?,?)",
            (
                template_id, template_version_id, template_version, json.dumps(validation_snapshot, ensure_ascii=False, sort_keys=True),
                project_code, requested_by, json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest, priority, idempotency_key, max_attempts, now, now, now,
            ),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def queued_candidate(self, capabilities: Iterable[str], now: str) -> sqlite3.Row | None:
        capability_list = sorted(set(capabilities))
        params: list[Any] = [now]
        condition = ""
        if capability_list:
            placeholders = ",".join("?" for _ in capability_list)
            condition = f" AND COALESCE(tv.algorithm, tpl.algorithm) IN ({placeholders})"
            params.extend(capability_list)
        return self.connection.execute(
            "SELECT t.*,COALESCE(tv.algorithm, tpl.algorithm) AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id LEFT JOIN compute_template_versions tv ON tv.id=t.template_version_id WHERE t.status='queued' AND t.available_at<=?" + condition + " ORDER BY t.priority DESC,t.created_at ASC,t.id ASC LIMIT 1",
            params,
        ).fetchone()

    def result_versions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_results WHERE task_id=? ORDER BY version", (task_id,)).fetchall()]

    def interventions(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM compute_interventions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]

    def add_intervention(self, *, task_id: int, actor: str, action: str, reason: str, before: dict[str, Any], after: dict[str, Any], batch_key: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO compute_interventions(task_id,actor,action,reason,before_json,after_json,batch_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, actor, action, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), batch_key, now),
        )

    def list_tasks(self, *, status: str | None, project_code: str | None, requested_by: str | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("t.status=?")
            values.append(status)
        if project_code:
            clauses.append("t.project_code=?")
            values.append(project_code)
        if requested_by:
            clauses.append("t.requested_by=?")
            values.append(requested_by)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT t.*,tpl.code AS template_code,COALESCE(tv.algorithm, tpl.algorithm) AS template_algorithm FROM compute_tasks t JOIN compute_templates tpl ON tpl.id=t.template_id LEFT JOIN compute_template_versions tv ON tv.id=t.template_version_id" + where + " ORDER BY t.priority DESC,t.created_at DESC,t.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
