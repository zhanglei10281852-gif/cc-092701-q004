from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.compute.repository import ComputeRepository
from app.compute.versioning import build_validation_snapshot, content_digest, content_from_row, diff_content, version_content, version_state
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class ComputeOperationsService:
    """管理计算模板、配额、任务租约、结果版本和人工干预。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ComputeRepository(self.connection)

    def list_templates(self) -> list[dict[str, Any]]:
        now = to_storage(self.clock.now())
        items: list[dict[str, Any]] = []
        for template in self.repository.active_templates():
            version = self.repository.effective_version(template["id"], now)
            if version is None:
                continue
            item = dict(template)
            item.update(
                {
                    "name": version["name"],
                    "algorithm": version["algorithm"],
                    "parameter_schema_json": version["parameter_schema_json"],
                    "default_parameters_json": version["default_parameters_json"],
                    "max_runtime_seconds": version["max_runtime_seconds"],
                    "max_attempts": version["max_attempts"],
                    "current_version": version["version"],
                    "current_version_id": version["id"],
                    "effective_at": version["effective_at"],
                }
            )
            items.append(item)
        return items

    def create_template(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_schema(payload["parameter_schema"], payload["default_parameters"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            if repository.template_by_code(payload["code"]):
                raise ConflictError("参数模板编码已存在")
            template = repository.create_template(
                code=payload["code"], name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], defaults=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                created_by=actor, now=now,
            )
            content = version_content(
                name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], default_parameters=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
            )
            repository.insert_version(
                template_id=template["id"], version=1, status="published",
                name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], default_parameters=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                base_version=None, effective_at=now, published_by=actor, published_at=now,
                digest=content_digest(content), actor=actor, now=now,
            )
            return {**template, "current_version": 1}

    def set_quota(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return ComputeRepository(connection).upsert_quota(actor=actor, now=now, **payload)

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = repository.template_by_code(payload["template_code"])
            if template is None or not template["active"]:
                raise NotFoundError("参数模板不存在或已经停用")
            version = repository.effective_version(template["id"], now)
            if version is None:
                raise ConflictError("参数模板暂无已生效的发布版本")
            parameters = self._validate_parameters(version, payload["parameters"])
            existing = repository.task_by_idempotency(payload["requested_by"], payload["idempotency_key"])
            parameter_digest = digest(parameters)
            if existing is not None:
                if existing["parameter_digest"] != parameter_digest:
                    raise ConflictError("同一幂等键对应了不同的计算参数")
                return dict(repository.task_by_id(existing["id"]))
            self._check_quota(repository, payload["requested_by"], now_value)
            snapshot = build_validation_snapshot(
                template_code=template["code"], version_row=dict(version),
                supplied_parameters=payload["parameters"], captured_at=now,
            )
            return repository.create_task(
                template_id=template["id"], template_version_id=version["id"], template_version=version["version"],
                validation_snapshot=snapshot, project_code=payload["project_code"],
                requested_by=payload["requested_by"], parameters=parameters,
                parameter_digest=parameter_digest, priority=payload["priority"],
                idempotency_key=payload["idempotency_key"], max_attempts=version["max_attempts"], now=now,
            )

    def list_tasks(self, *, status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=max(1, min(limit, 500)))

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.repository.task_by_id(task_id)
        if row is None:
            raise NotFoundError("计算任务不存在")
        result = dict(row)
        result["validation_snapshot"] = json.loads(result["validation_snapshot_json"]) if result.get("validation_snapshot_json") else None
        result["results"] = self.repository.result_versions(task_id)
        result["interventions"] = self.repository.interventions(task_id)
        return result

    def claim(self, worker_id: str, capabilities: list[str], lease_seconds: int) -> dict[str, Any] | None:
        now_value = self.clock.now()
        now = to_storage(now_value)
        lease_until = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            candidate = repository.queued_candidate(capabilities, now)
            if candidate is None:
                return None
            cursor = connection.execute(
                "UPDATE compute_tasks SET status='running',attempt_count=attempt_count+1,lease_owner=?,lease_expires_at=?,started_at=COALESCE(started_at,?),updated_at=?,version=version+1 WHERE id=? AND status='queued'",
                (worker_id, lease_until, now, now, candidate["id"]),
            )
            if cursor.rowcount != 1:
                return None
            return dict(repository.task_by_id(candidate["id"]))

    def heartbeat(self, task_id: int, worker_id: str, lease_seconds: int) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        expires = to_storage(now_value + timedelta(seconds=lease_seconds))
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE compute_tasks SET lease_expires_at=?,updated_at=?,version=version+1 WHERE id=? AND status='running' AND lease_owner=?",
                (expires, now, task_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("任务未由当前工作者持有")
            return dict(ComputeRepository(connection).task_by_id(task_id))

    def complete(self, task_id: int, worker_id: str, result: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] != "running" or task["lease_owner"] != worker_id:
                raise ConflictError("任务未由当前工作者持有")
            version = int(connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM compute_results WHERE task_id=?", (task_id,)).fetchone()[0])
            connection.execute(
                "INSERT INTO compute_results(task_id,version,result_json,metrics_json,result_digest,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (task_id, version, json.dumps(result, ensure_ascii=False, sort_keys=True), json.dumps(metrics, ensure_ascii=False, sort_keys=True), digest({"result": result, "metrics": metrics}), worker_id, now),
            )
            connection.execute(
                "UPDATE compute_tasks SET status='succeeded',current_result_version=?,lease_owner='',lease_expires_at='',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (version, now, now, task_id),
            )
            return dict(repository.task_by_id(task_id))

    def fail(self, task_id: int, worker_id: str, error_code: str, message: str, retryable: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            if task["status"] != "running" or task["lease_owner"] != worker_id:
                raise ConflictError("任务未由当前工作者持有")
            can_retry = retryable and int(task["attempt_count"]) < int(task["max_attempts"])
            status = "queued" if can_retry else "failed"
            delay = min(300, 2 ** max(0, int(task["attempt_count"]) - 1)) if can_retry else 0
            available = to_storage(now_value + timedelta(seconds=delay))
            connection.execute(
                "UPDATE compute_tasks SET status=?,available_at=?,lease_owner='',lease_expires_at='',last_error_code=?,last_error_message=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                (status, available, error_code, message[:2000], None if can_retry else now, now, task_id),
            )
            return dict(repository.task_by_id(task_id))

    def cancel(self, task_id: int, actor: str, reason: str, batch_key: str = "") -> dict[str, Any]:
        return self._intervene(task_id, actor, reason, "cancel", batch_key, self._cancel_mutation)

    def retry(self, task_id: int, actor: str, reason: str, priority: int | None = None, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"failed", "cancelled"}:
                raise ConflictError("只有失败或已取消任务可以人工重试")
            chosen = task["priority"] if priority is None else priority
            connection.execute("UPDATE compute_tasks SET status='queued',priority=?,available_at=?,lease_owner='',lease_expires_at='',finished_at=NULL,updated_at=?,version=version+1 WHERE id=?", (chosen, now, now, task["id"]))
        return self._intervene(task_id, actor, reason, "retry", batch_key, mutate)

    def set_priority(self, task_id: int, actor: str, reason: str, priority: int, batch_key: str = "") -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
            if task["status"] not in {"queued", "running"}:
                raise ConflictError("只有排队或运行中的任务可以调整优先级")
            connection.execute("UPDATE compute_tasks SET priority=?,updated_at=?,version=version+1 WHERE id=?", (priority, now, task["id"]))
        return self._intervene(task_id, actor, reason, "priority", batch_key, mutate)

    def batch_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        batch_key = digest({"actor": payload["actor"], "task_ids": payload["task_ids"], "operation": payload["operation"], "reason": payload["reason"]})
        succeeded: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for task_id in list(dict.fromkeys(payload["task_ids"])):
            try:
                if payload["operation"] == "cancel":
                    value = self.cancel(task_id, payload["actor"], payload["reason"], batch_key)
                elif payload["operation"] == "retry":
                    value = self.retry(task_id, payload["actor"], payload["reason"], payload.get("priority"), batch_key)
                else:
                    value = self.set_priority(task_id, payload["actor"], payload["reason"], int(payload["priority"]), batch_key)
                succeeded.append({"task_id": task_id, "status": value["status"], "version": value["version"]})
            except (ConflictError, NotFoundError) as exc:
                failed.append({"task_id": task_id, "code": exc.code, "message": exc.message})
        return {"batch_key": batch_key, "succeeded": succeeded, "failed": failed}

    def recover_expired(self, actor: str = "recovery-worker") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        recovered: list[int] = []
        exhausted: list[int] = []
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            rows = connection.execute("SELECT * FROM compute_tasks WHERE status='running' AND lease_expires_at<>'' AND lease_expires_at<? ORDER BY id", (now,)).fetchall()
            for task in rows:
                before = dict(task)
                if int(task["attempt_count"]) < int(task["max_attempts"]):
                    status, finished_at = "queued", None
                    recovered.append(int(task["id"]))
                else:
                    status, finished_at = "failed", now
                    exhausted.append(int(task["id"]))
                connection.execute(
                    "UPDATE compute_tasks SET status=?,lease_owner='',lease_expires_at='',available_at=?,last_error_code='lease_expired',last_error_message='工作者租约已过期',finished_at=?,updated_at=?,version=version+1 WHERE id=?",
                    (status, now, finished_at, now, task["id"]),
                )
                after = dict(repository.task_by_id(task["id"]))
                repository.add_intervention(task_id=task["id"], actor=actor, action="lease_recovery", reason="租约过期自动恢复", before=before, after=after, batch_key="", now=now)
        return {"recovered": recovered, "exhausted": exhausted}

    def summary(self) -> dict[str, Any]:
        rows = self.connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks GROUP BY status ORDER BY status").fetchall()
        oldest = self.connection.execute("SELECT MIN(created_at) FROM compute_tasks WHERE status='queued'").fetchone()[0]
        return {"states": {row["status"]: row["amount"] for row in rows}, "oldest_queued_at": oldest, "templates": len(self.repository.active_templates())}

    # ---- 模板版本生命周期：草稿 -> 发布 -> 生效/历史 ----

    def list_versions(self, code: str) -> list[dict[str, Any]]:
        template = self._template_or_404(self.repository, code)
        return self._version_views(self.repository, template, self.repository.versions_for_template(template["id"]))

    def get_version(self, code: str, version: int) -> dict[str, Any]:
        template = self._template_or_404(self.repository, code)
        row = self._version_or_404(self.repository, template["id"], version)
        return self._version_views(self.repository, template, [row])[0]

    def create_draft(self, code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = self._template_or_404(repository, code)
            base_version = payload.get("base_version")
            if base_version is not None:
                base = repository.version_by_number(template["id"], base_version)
                if base is None:
                    raise NotFoundError("基线版本不存在")
            else:
                base = repository.effective_version(template["id"], now) or repository.latest_published_version(template["id"])
                if base is None:
                    raise NotFoundError("模板还没有可作为基线的发布版本")
            if base["status"] != "published":
                raise ValidationError("基线版本必须是已发布版本")
            row = repository.insert_version(
                template_id=template["id"], version=repository.next_version_number(template["id"]), status="draft",
                name=base["name"], algorithm=base["algorithm"],
                parameter_schema=json.loads(base["parameter_schema_json"]), default_parameters=json.loads(base["default_parameters_json"]),
                max_runtime_seconds=base["max_runtime_seconds"], max_attempts=base["max_attempts"],
                base_version=base["version"], effective_at=None, published_by=None, published_at=None,
                digest=base["content_digest"], actor=actor, now=now,
            )
            return self._version_views(repository, template, [row])[0]

    def update_draft(self, code: str, version: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        del actor  # 操作者由审计调用方记录，草稿内容本身不区分编辑人
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = self._template_or_404(repository, code)
            row = self._version_or_404(repository, template["id"], version)
            if row["status"] != "draft":
                raise ConflictError("只有草稿版本可以编辑")
            self._validate_schema(payload["parameter_schema"], payload["default_parameters"])
            content = version_content(
                name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], default_parameters=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
            )
            repository.update_draft_content(
                row["id"], name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], default_parameters=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                digest=content_digest(content), now=now,
            )
            return self._version_views(repository, template, [repository.version_by_id(row["id"])])[0]

    def publish_version(self, code: str, version: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = self._template_or_404(repository, code)
            row = self._version_or_404(repository, template["id"], version)
            if row["status"] != "draft":
                raise ConflictError("只有草稿版本可以发布")
            # 发布前复核内容；任何失败都会回滚整个事务，不影响当前有效版本
            self._validate_schema(json.loads(row["parameter_schema_json"]), json.loads(row["default_parameters_json"]))
            effective_at_value = now_value
            scheduled_at = payload.get("effective_at")
            if scheduled_at is not None:
                if scheduled_at.tzinfo is None:
                    scheduled_at = scheduled_at.replace(tzinfo=UTC)
                scheduled_at = scheduled_at.astimezone(UTC)
                if scheduled_at <= now_value:
                    raise ValidationError("计划的生效时间必须晚于当前时间")
                effective_at_value = scheduled_at
            current = repository.effective_version(template["id"], now)
            current_number = current["version"] if current is not None else None
            expected = payload.get("expected_effective_version")
            if expected is not None and expected != current_number:
                raise ConflictError("模板当前有效版本与预期不符，请刷新后重试", context={"expected": expected, "current": current_number})
            if not payload.get("force") and current_number is not None and row["base_version"] is not None and row["base_version"] != current_number:
                raise ConflictError(
                    "草稿基线版本已不是当前有效版本，存在并发发布冲突",
                    context={"base_version": row["base_version"], "current_effective_version": current_number},
                )
            changed = repository.mark_version_published(row["id"], effective_at=to_storage(effective_at_value), published_by=actor, now=now)
            if changed != 1:
                raise ConflictError("版本状态已变化，发布失败")
            return self._version_views(repository, template, [repository.version_by_id(row["id"])])[0]

    def discard_draft(self, code: str, version: int, actor: str) -> dict[str, Any]:
        del actor
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template = self._template_or_404(repository, code)
            row = self._version_or_404(repository, template["id"], version)
            if row["status"] != "draft":
                raise ConflictError("已发布版本不可删除，引用它的历史任务需要按原规则回放")
            repository.delete_version(row["id"])
            return {"template_code": code, "version": version, "deleted": True}

    def diff_versions(self, code: str, from_version: int, to_version: int) -> dict[str, Any]:
        template = self._template_or_404(self.repository, code)
        rows = {row["version"]: row for row in self.repository.versions_for_template(template["id"])}
        missing = [number for number in (from_version, to_version) if number not in rows]
        if missing:
            raise NotFoundError("模板版本不存在", context={"missing": missing})
        before, after = rows[from_version], rows[to_version]
        changes = diff_content(content_from_row(dict(before)), content_from_row(dict(after)))
        views = {view["version"]: view for view in self._version_views(self.repository, template, [before, after])}
        return {
            "template_code": code,
            "from": views[from_version],
            "to": views[to_version],
            "identical": not changes,
            "changes": changes,
        }

    def revalidate_task(self, task_id: int) -> dict[str, Any]:
        """按任务创建时冻结的校验输入回放校验，重启或模板演进后结果仍一致。"""
        task = self.repository.task_by_id(task_id)
        if task is None:
            raise NotFoundError("计算任务不存在")
        now = to_storage(self.clock.now())
        source = "snapshot"
        snapshot = json.loads(task["validation_snapshot_json"]) if task["validation_snapshot_json"] else None
        if snapshot is None:
            version_row = self.repository.version_by_id(task["template_version_id"]) if task["template_version_id"] else None
            if version_row is None:
                raise ConflictError("任务缺少校验快照，无法按原规则复核")
            snapshot = build_validation_snapshot(
                template_code=task["template_code"], version_row=dict(version_row),
                supplied_parameters=json.loads(task["parameters_json"]), captured_at=task["created_at"],
            )
            source = "version_record"
        errors: list[str] = []
        normalized: dict[str, Any] | None = None
        try:
            normalized = self._validate_against_schema(snapshot["parameter_schema"], snapshot["default_parameters"], snapshot["supplied_parameters"])
        except ValidationError as exc:
            errors.append(exc.message)
        stored_parameters = json.loads(task["parameters_json"])
        recomputed_digest = digest(normalized) if normalized is not None else None
        record_digest = None
        if task["template_version_id"]:
            record = self.repository.version_by_id(task["template_version_id"])
            record_digest = record["content_digest"] if record is not None else None
        return {
            "task_id": task_id,
            "template_code": snapshot["template_code"],
            "template_version": snapshot["template_version"],
            "source": source,
            "valid": normalized is not None,
            "errors": errors,
            "consistent": normalized is not None and normalized == stored_parameters and recomputed_digest == task["parameter_digest"],
            "normalized_parameters": normalized,
            "stored_parameters": stored_parameters,
            "parameter_digest": recomputed_digest,
            "stored_parameter_digest": task["parameter_digest"],
            "snapshot_digest": snapshot.get("content_digest"),
            "version_record_digest": record_digest,
            "version_record_matches_snapshot": record_digest is None or record_digest == snapshot.get("content_digest"),
            "checked_at": now,
        }

    def _template_or_404(self, repository: ComputeRepository, code: str) -> sqlite3.Row:
        template = repository.template_by_code(code)
        if template is None:
            raise NotFoundError("参数模板不存在")
        return template

    @staticmethod
    def _version_or_404(repository: ComputeRepository, template_id: int, version: int) -> sqlite3.Row:
        row = repository.version_by_number(template_id, version)
        if row is None:
            raise NotFoundError("模板版本不存在")
        return row

    def _version_views(self, repository: ComputeRepository, template: sqlite3.Row, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        now = to_storage(self.clock.now())
        current = repository.effective_version(template["id"], now)
        current_number = current["version"] if current is not None else None
        views: list[dict[str, Any]] = []
        for row in rows:
            views.append(
                {
                    "template_code": template["code"],
                    "version": row["version"],
                    "status": row["status"],
                    "state": version_state(status=row["status"], effective_at=row["effective_at"], is_current=row["version"] == current_number, now=now),
                    "name": row["name"],
                    "algorithm": row["algorithm"],
                    "parameter_schema": json.loads(row["parameter_schema_json"]),
                    "default_parameters": json.loads(row["default_parameters_json"]),
                    "max_runtime_seconds": row["max_runtime_seconds"],
                    "max_attempts": row["max_attempts"],
                    "base_version": row["base_version"],
                    "effective_at": row["effective_at"],
                    "published_by": row["published_by"],
                    "published_at": row["published_at"],
                    "content_digest": row["content_digest"],
                    "task_count": repository.count_version_tasks(row["id"]),
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return views

    def _intervene(self, task_id: int, actor: str, reason: str, action: str, batch_key: str, mutation: Callable[[sqlite3.Connection, sqlite3.Row, str], None]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            task = repository.task_by_id(task_id)
            if task is None:
                raise NotFoundError("计算任务不存在")
            before = dict(task)
            mutation(connection, task, now)
            after = dict(repository.task_by_id(task_id))
            repository.add_intervention(task_id=task_id, actor=actor, action=action, reason=reason, before=before, after=after, batch_key=batch_key, now=now)
            return after

    @staticmethod
    def _cancel_mutation(connection: sqlite3.Connection, task: sqlite3.Row, now: str) -> None:
        if task["status"] not in {"queued", "running"}:
            raise ConflictError("当前任务状态不允许取消")
        status = "cancel_requested" if task["status"] == "running" else "cancelled"
        connection.execute("UPDATE compute_tasks SET status=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?", (status, None if status == "cancel_requested" else now, now, task["id"]))

    def _check_quota(self, repository: ComputeRepository, requested_by: str, now: datetime) -> None:
        quota = repository.quota("user", requested_by)
        if quota is None:
            return
        states = repository.count_user_states(requested_by)
        if states.get("queued", 0) >= int(quota["max_queued"]):
            raise ConflictError("用户排队任务配额已用尽")
        if states.get("running", 0) >= int(quota["max_running"]):
            raise ConflictError("用户运行任务配额已用尽")
        day_start = to_storage(now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0))
        if repository.count_user_submissions_since(requested_by, day_start) >= int(quota["daily_submissions"]):
            raise ConflictError("用户当日提交配额已用尽")

    @staticmethod
    def _validate_schema(schema: dict[str, dict[str, Any]], defaults: dict[str, Any]) -> None:
        if not schema:
            raise ValidationError("参数模板至少包含一个参数")
        allowed = {"integer", "number", "string", "boolean"}
        for name, rule in schema.items():
            if not name or not isinstance(rule, dict) or rule.get("type") not in allowed:
                raise ValidationError(f"参数 {name or '<empty>'} 的规则不合法")
        if set(defaults) - set(schema):
            raise ValidationError("默认值包含未声明参数")

    def _validate_parameters(self, template: sqlite3.Row, supplied: dict[str, Any]) -> dict[str, Any]:
        return self._validate_against_schema(json.loads(template["parameter_schema_json"]), json.loads(template["default_parameters_json"]), supplied)

    @staticmethod
    def _validate_against_schema(schema: dict[str, dict[str, Any]], defaults: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
        values = {**defaults, **supplied}
        unknown = set(values) - set(schema)
        if unknown:
            raise ValidationError("包含模板未声明的参数", context={"parameters": sorted(unknown)})
        normalized: dict[str, Any] = {}
        for name, rule in schema.items():
            if name not in values:
                if rule.get("required"):
                    raise ValidationError(f"缺少必填参数：{name}")
                continue
            value = values[name]
            kind = rule["type"]
            valid = {"integer": isinstance(value, int) and not isinstance(value, bool), "number": isinstance(value, (int, float)) and not isinstance(value, bool), "string": isinstance(value, str), "boolean": isinstance(value, bool)}[kind]
            if not valid:
                raise ValidationError(f"参数 {name} 类型不正确")
            if rule.get("minimum") is not None and value < rule["minimum"]:
                raise ValidationError(f"参数 {name} 小于允许的最小值")
            if rule.get("maximum") is not None and value > rule["maximum"]:
                raise ValidationError(f"参数 {name} 大于允许的最大值")
            if rule.get("choices") and value not in rule["choices"]:
                raise ValidationError(f"参数 {name} 不在允许的选项中")
            normalized[name] = value
        return normalized
