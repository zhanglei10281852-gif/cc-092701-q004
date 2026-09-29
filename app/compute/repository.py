from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

CONTENT_FIELDS = (
    "name",
    "algorithm",
    "parameter_schema_json",
    "default_parameters_json",
    "max_runtime_seconds",
    "max_attempts",
)


class ComputeRepository:
    """封装计算任务运营领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ------------------------------------------------------------------
    # 模板编码与版本
    # ------------------------------------------------------------------
    def template_code_row(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_template_codes WHERE code=?", (code,)).fetchone()

    def create_template_code(self, *, code: str, created_by: str, now: str) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO compute_template_codes(code,current_version,created_by,created_at,updated_at) VALUES(?,NULL,?,?,?)",
            (code, created_by, now, now),
        )
        return self.template_code_row_by_id(cursor.lastrowid)

    def template_code_row_by_id(self, template_code_id: int) -> sqlite3.Row:
        return self.connection.execute("SELECT * FROM compute_template_codes WHERE id=?", (template_code_id,)).fetchone()

    def template_version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_template_versions WHERE id=?", (version_id,)).fetchone()

    def template_version(self, template_code_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_template_versions WHERE template_code_id=? AND version=?",
            (template_code_id, version),
        ).fetchone()

    def latest_template_version(self, template_code_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_template_versions WHERE template_code_id=? ORDER BY version DESC LIMIT 1",
            (template_code_id,),
        ).fetchone()

    def draft_template_version(self, template_code_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_template_versions WHERE template_code_id=? AND status='draft' ORDER BY version DESC LIMIT 1",
            (template_code_id,),
        ).fetchone()

    def list_template_versions(self, code: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT tv.*,p.effective_at AS publication_effective_at,p.status AS publication_status,p.activated_at "
            "FROM compute_template_versions tv "
            "JOIN compute_template_codes tc ON tc.id=tv.template_code_id "
            "LEFT JOIN compute_template_publications p ON p.template_code_id=tv.template_code_id AND p.version=tv.version "
            "WHERE tc.code=? ORDER BY tv.version",
            (code,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_template_codes(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT tc.*,tv.version AS active_version,tv.name,tv.algorithm,tv.parameter_schema_json,"
            "tv.default_parameters_json,tv.max_runtime_seconds,tv.max_attempts,tv.published_at "
            "FROM compute_template_codes tc "
            "JOIN compute_template_publications p ON p.template_code_id=tc.id AND p.status='active' "
            "JOIN compute_template_versions tv ON tv.template_code_id=tc.id AND tv.version=p.version "
            "ORDER BY tc.code"
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_template_version(
        self,
        *,
        template_code_id: int,
        code: str,
        version: int,
        name: str,
        algorithm: str,
        parameter_schema: dict[str, Any],
        defaults: dict[str, Any],
        max_runtime_seconds: int,
        max_attempts: int,
        content_digest: str,
        created_by: str,
        now: str,
    ) -> sqlite3.Row:
        cursor = self.connection.execute(
            "INSERT INTO compute_template_versions(template_code_id,code,version,name,algorithm,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,status,content_digest,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,'draft',?,?,?,?)",
            (
                template_code_id, code, version, name, algorithm,
                json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True),
                json.dumps(defaults, ensure_ascii=False, sort_keys=True),
                max_runtime_seconds, max_attempts, content_digest, created_by, now, now,
            ),
        )
        return self.template_version_by_id(cursor.lastrowid)

    def update_draft_template_version(
        self,
        *,
        version_id: int,
        name: str,
        algorithm: str,
        parameter_schema: dict[str, Any],
        defaults: dict[str, Any],
        max_runtime_seconds: int,
        max_attempts: int,
        content_digest: str,
        now: str,
    ) -> None:
        self.connection.execute(
            "UPDATE compute_template_versions SET name=?,algorithm=?,parameter_schema_json=?,default_parameters_json=?,"
            "max_runtime_seconds=?,max_attempts=?,content_digest=?,updated_at=? WHERE id=? AND status='draft'",
            (
                name, algorithm,
                json.dumps(parameter_schema, ensure_ascii=False, sort_keys=True),
                json.dumps(defaults, ensure_ascii=False, sort_keys=True),
                max_runtime_seconds, max_attempts, content_digest, now, version_id,
            ),
        )

    def mark_version_published(self, *, version_id: int, now: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE compute_template_versions SET status='published',published_at=?,updated_at=? WHERE id=? AND status='draft'",
            (now, now, version_id),
        )
        return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # 发布调度
    # ------------------------------------------------------------------
    def active_publication(self, template_code_id: int, now: str) -> sqlite3.Row | None:
        """当前时刻对查询生效的发布：active，或已到期但尚未惰性切换的 scheduled。"""
        return self.connection.execute(
            "SELECT * FROM compute_template_publications WHERE template_code_id=? AND status IN ('active','scheduled') "
            "AND effective_at<=? ORDER BY effective_at DESC,id DESC LIMIT 1",
            (template_code_id, now),
        ).fetchone()

    def future_publication(self, template_code_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_template_publications WHERE template_code_id=? AND status='scheduled' AND effective_at>? "
            "ORDER BY effective_at,id LIMIT 1",
            (template_code_id, now),
        ).fetchone()

    def due_scheduled_publications(self, now: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM compute_template_publications WHERE status='scheduled' AND effective_at<=? ORDER BY effective_at,id",
            (now,),
        ).fetchall()

    def insert_publication(self, *, template_code_id: int, version: int, effective_at: str, published_by: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO compute_template_publications(template_code_id,version,effective_at,status,published_by,created_at) "
            "VALUES(?,?,?, 'scheduled',?,?)",
            (template_code_id, version, effective_at, published_by, now),
        )
        return int(cursor.lastrowid)

    def activate_publication(self, connection: sqlite3.Connection | None, *, template_code_id: int, version: int, now: str) -> bool:
        """把指定发布切换为 active，旧 active 置为 superseded。返回是否真的完成切换。

        必须先腾退旧 active 与其他 scheduled，再激活目标版本，
        否则会与同表的部分唯一索引（至多一个 active/scheduled）冲突。
        """
        connection = connection or self.connection
        candidate = connection.execute(
            "SELECT 1 FROM compute_template_publications WHERE template_code_id=? AND version=? AND status IN ('scheduled','active')",
            (template_code_id, version),
        ).fetchone()
        if candidate is None:
            return False
        connection.execute(
            "UPDATE compute_template_publications SET status='superseded',deactivated_at=? "
            "WHERE template_code_id=? AND status='active' AND version<>?",
            (now, template_code_id, version),
        )
        connection.execute(
            "UPDATE compute_template_publications SET status='cancelled',deactivated_at=? "
            "WHERE template_code_id=? AND status='scheduled' AND version<>?",
            (now, template_code_id, version),
        )
        connection.execute(
            "UPDATE compute_template_publications SET status='active',activated_at=? "
            "WHERE template_code_id=? AND version=? AND status IN ('scheduled','active')",
            (now, template_code_id, version),
        )
        connection.execute(
            "UPDATE compute_template_codes SET current_version=?,updated_at=? WHERE id=?",
            (version, now, template_code_id),
        )
        return True

    def cancel_scheduled_publication(self, *, template_code_id: int, now: str) -> int:
        cursor = self.connection.execute(
            "UPDATE compute_template_publications SET status='cancelled',deactivated_at=? "
            "WHERE template_code_id=? AND status='scheduled'",
            (now, template_code_id),
        )
        return cursor.rowcount

    def publication_for_version(self, template_code_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM compute_template_publications WHERE template_code_id=? AND version=?",
            (template_code_id, version),
        ).fetchone()

    def tasks_referencing_version(self, version_id: int) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) FROM compute_tasks WHERE template_version_id=?", (version_id,)
        ).fetchone()[0])

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    def task_by_id(self, task_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_tasks WHERE id=?", (task_id,)).fetchone()

    def task_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM compute_tasks WHERE requested_by=? AND idempotency_key=?", (requested_by, key)).fetchone()

    def create_task(self, *, template_version: sqlite3.Row, project_code: str, requested_by: str, parameters: dict[str, Any], parameter_digest: str, validation_snapshot: dict[str, Any], priority: int, idempotency_key: str, max_attempts: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO compute_tasks(template_version_id,template_code,template_version,template_algorithm,project_code,requested_by,parameters_json,parameter_digest,validation_snapshot_json,priority,idempotency_key,status,attempt_count,max_attempts,available_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?, 'queued',0,?,?,?,?)",
            (
                template_version["id"], template_version["code"], template_version["version"],
                template_version["algorithm"], project_code, requested_by,
                json.dumps(parameters, ensure_ascii=False, sort_keys=True), parameter_digest,
                json.dumps(validation_snapshot, ensure_ascii=False, sort_keys=True),
                priority, idempotency_key, max_attempts, now, now, now,
            ),
        )
        return dict(self.task_by_id(cursor.lastrowid))

    def queued_candidate(self, capabilities: Iterable[str], now: str) -> sqlite3.Row | None:
        capability_list = sorted(set(capabilities))
        params: list[Any] = [now]
        condition = ""
        if capability_list:
            placeholders = ",".join("?" for _ in capability_list)
            condition = f" AND t.template_algorithm IN ({placeholders})"
            params.extend(capability_list)
        return self.connection.execute(
            "SELECT t.* FROM compute_tasks t WHERE t.status='queued' AND t.available_at<=?" + condition + " ORDER BY t.priority DESC,t.created_at ASC,t.id ASC LIMIT 1",
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
            "SELECT t.* FROM compute_tasks t" + where + " ORDER BY t.priority DESC,t.created_at DESC,t.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
