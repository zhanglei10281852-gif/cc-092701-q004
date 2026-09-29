from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from app.compute.repository import ComputeRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


SCALAR_TEMPLATE_FIELDS = ("name", "algorithm", "max_runtime_seconds", "max_attempts")


class ComputeOperationsService:
    """管理计算模板的草稿/发布版本、配额、任务租约、结果版本和人工干预。

    模板版本一旦发布即不可变：任务在提交时冻结完整校验输入快照，
    此后所有复核只依赖快照，与当前有效版本无关。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = ComputeRepository(self.connection)

    # ------------------------------------------------------------------
    # 模板：查询当前有效版本
    # ------------------------------------------------------------------
    def list_templates(self) -> list[dict[str, Any]]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            self._activate_due(repository, now)
            return repository.list_template_codes()

    def get_template(self, code: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            self._activate_due_for(repository, template_code["id"], now)
            publication = repository.active_publication(template_code["id"], now)
            if publication is None:
                raise NotFoundError("参数模板尚未发布任何有效版本")
            version = repository.template_version(template_code["id"], publication["version"])
            return self._template_version_view(repository, dict(version))

    # ------------------------------------------------------------------
    # 模板：草稿编辑（可反复修改，不影响任何任务与查询）
    # ------------------------------------------------------------------
    def create_template(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_schema(payload["parameter_schema"], payload["default_parameters"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            if repository.template_code_row(payload["code"]):
                raise ConflictError("参数模板编码已存在")
            template_code = repository.create_template_code(code=payload["code"], created_by=actor, now=now)
            content_digest = self._content_digest(payload)
            row = repository.insert_template_version(
                template_code_id=template_code["id"], code=payload["code"], version=1,
                name=payload["name"], algorithm=payload["algorithm"],
                parameter_schema=payload["parameter_schema"], defaults=payload["default_parameters"],
                max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                content_digest=content_digest, created_by=actor, now=now,
            )
            return self._template_version_view(repository, dict(row))

    def get_draft(self, code: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            self._activate_due_for(repository, template_code["id"], now)
            draft = repository.draft_template_version(template_code["id"])
            if draft is None:
                raise NotFoundError("该模板当前没有可编辑的草稿")
            return self._template_version_view(repository, dict(draft))

    def save_draft(self, code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_schema(payload["parameter_schema"], payload["default_parameters"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            content_digest = self._content_digest(payload)
            draft = repository.draft_template_version(template_code["id"])
            if draft is None:
                latest = repository.latest_template_version(template_code["id"])
                next_version = 1 if latest is None else int(latest["version"]) + 1
                row = repository.insert_template_version(
                    template_code_id=template_code["id"], code=code, version=next_version,
                    name=payload["name"], algorithm=payload["algorithm"],
                    parameter_schema=payload["parameter_schema"], defaults=payload["default_parameters"],
                    max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                    content_digest=content_digest, created_by=actor, now=now,
                )
            else:
                repository.update_draft_template_version(
                    version_id=draft["id"], name=payload["name"], algorithm=payload["algorithm"],
                    parameter_schema=payload["parameter_schema"], defaults=payload["default_parameters"],
                    max_runtime_seconds=payload["max_runtime_seconds"], max_attempts=payload["max_attempts"],
                    content_digest=content_digest, now=now,
                )
                row = repository.template_version_by_id(draft["id"])
            return self._template_version_view(repository, dict(row))

    # ------------------------------------------------------------------
    # 模板：发布（立即或定时）
    # ------------------------------------------------------------------
    def publish_template(self, code: str, actor: str, effective_at: datetime | None) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        effective = to_storage(effective_at) if effective_at is not None else now
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            self._activate_due_for(repository, template_code["id"], now)
            draft = repository.draft_template_version(template_code["id"])
            if draft is None:
                raise ConflictError("该模板没有可发布的草稿")
            if effective_at is not None and effective_at <= now_value:
                effective = now
            try:
                if not repository.mark_version_published(version_id=draft["id"], now=now):
                    # 并发发布：草稿已被另一个事务发布，当前事务不再写入发布记录。
                    raise ConflictError("草稿已被其他发布操作处理，请刷新后重试")
                repository.insert_publication(
                    template_code_id=template_code["id"], version=draft["version"],
                    effective_at=effective, published_by=actor, now=now,
                )
                if effective <= now:
                    repository.activate_publication(
                        connection, template_code_id=template_code["id"], version=draft["version"], now=now,
                    )
            except sqlite3.IntegrityError as exc:
                # 唯一约束：同一模板同时只能有一个待生效发布。事务回滚，当前有效版本不变。
                raise ConflictError("该模板已存在待生效的发布，请先取消后再发布新版本") from exc
            publication_row = repository.publication_for_version(template_code["id"], draft["version"])
            view = self._template_version_view(repository, dict(repository.template_version_by_id(draft["id"])))
            view["publication"] = dict(publication_row)
            return view

    def cancel_scheduled_publication(self, code: str, actor: str) -> dict[str, Any]:
        del actor
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            scheduled = repository.future_publication(template_code["id"], now)
            if scheduled is None:
                raise NotFoundError("该模板没有未来生效的发布安排")
            repository.cancel_scheduled_publication(template_code_id=template_code["id"], now=now)
            return {"code": code, "cancelled_version": scheduled["version"], "effective_at": scheduled["effective_at"]}

    def activate_due_publications(self, actor: str = "publication-scheduler") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            activated = self._activate_due(repository, now, actor=actor)
        return {"activated": activated, "checked_at": now}

    # ------------------------------------------------------------------
    # 模板：版本历史与差异（版本不可删除，仅可回放）
    # ------------------------------------------------------------------
    def list_versions(self, code: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            self._activate_due_for(repository, template_code["id"], now)
            versions = repository.list_template_versions(code)
        return {"code": code, "items": versions}

    def get_version(self, code: str, version: int) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            self._activate_due_for(repository, template_code["id"], now)
            row = repository.template_version(template_code["id"], version)
            if row is None:
                raise NotFoundError("模板版本不存在")
            return self._template_version_view(repository, dict(row))

    def diff_versions(self, code: str, from_version: int, to_version: int) -> dict[str, Any]:
        if from_version == to_version:
            raise ValidationError("对比的两个版本必须不同")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = self._require_template_code(repository, code)
            before = repository.template_version(template_code["id"], from_version)
            after = repository.template_version(template_code["id"], to_version)
            if before is None or after is None:
                raise NotFoundError("模板版本不存在")
            return self._build_diff(before, after)

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------
    def set_quota(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return ComputeRepository(connection).upsert_quota(actor=actor, now=now, **payload)

    # ------------------------------------------------------------------
    # 任务：提交（冻结当时完整校验输入）
    # ------------------------------------------------------------------
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            template_code = repository.template_code_row(payload["template_code"])
            if template_code is None:
                raise NotFoundError("参数模板不存在或尚未发布")
            self._activate_due_for(repository, template_code["id"], now)
            publication = repository.active_publication(template_code["id"], now)
            if publication is None:
                raise NotFoundError("参数模板不存在或尚未发布")
            template = repository.template_version(template_code["id"], publication["version"])
            if template is None or template["status"] != "published":
                raise NotFoundError("参数模板不存在或尚未发布")
            schema = json.loads(template["parameter_schema_json"])
            defaults = json.loads(template["default_parameters_json"])
            parameters = self._validate_inputs(schema, defaults, payload["parameters"])
            existing = repository.task_by_idempotency(payload["requested_by"], payload["idempotency_key"])
            parameter_digest = digest(parameters)
            if existing is not None:
                if existing["parameter_digest"] != parameter_digest:
                    raise ConflictError("同一幂等键对应了不同的计算参数")
                return dict(repository.task_by_id(existing["id"]))
            self._check_quota(repository, payload["requested_by"], now_value)
            snapshot = {
                "template_code": template["code"],
                "template_version": template["version"],
                "template_version_id": template["id"],
                "name": template["name"],
                "algorithm": template["algorithm"],
                "parameter_schema": schema,
                "default_parameters": defaults,
                "parameters": parameters,
                "max_runtime_seconds": template["max_runtime_seconds"],
                "max_attempts": template["max_attempts"],
                "captured_at": now,
            }
            return repository.create_task(
                template_version=template, project_code=payload["project_code"],
                requested_by=payload["requested_by"], parameters=parameters,
                parameter_digest=parameter_digest, validation_snapshot=snapshot,
                priority=payload["priority"], idempotency_key=payload["idempotency_key"],
                max_attempts=template["max_attempts"], now=now,
            )

    def list_tasks(self, *, status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=max(1, min(limit, 500)))

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.repository.task_by_id(task_id)
        if row is None:
            raise NotFoundError("计算任务不存在")
        result = dict(row)
        result["validation_snapshot"] = json.loads(result.pop("validation_snapshot_json"))
        result["results"] = self.repository.result_versions(task_id)
        result["interventions"] = self.repository.interventions(task_id)
        return result

    def replay_task_validation(self, task_id: int) -> dict[str, Any]:
        """按任务提交时冻结的快照重新复核参数，不读取当前有效模板版本。

        服务重启后同样可用：所有规则均来自任务行内的快照，
        历史任务永远按原规则复核。
        """
        row = self.repository.task_by_id(task_id)
        if row is None:
            raise NotFoundError("计算任务不存在")
        snapshot = json.loads(row["validation_snapshot_json"])
        parameters = self._validate_inputs(
            snapshot["parameter_schema"], snapshot["default_parameters"], snapshot["parameters"],
        )
        replayed_digest = digest(parameters)
        return {
            "task_id": task_id,
            "template_code": snapshot["template_code"],
            "template_version": snapshot["template_version"],
            "captured_at": snapshot["captured_at"],
            "replayed_at": to_storage(self.clock.now()),
            "valid": replayed_digest == row["parameter_digest"],
            "parameter_digest": row["parameter_digest"],
            "replayed_digest": replayed_digest,
        }

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
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = ComputeRepository(connection)
            self._activate_due(repository, now)
            rows = connection.execute("SELECT status,COUNT(*) AS amount FROM compute_tasks GROUP BY status ORDER BY status").fetchall()
            oldest = connection.execute("SELECT MIN(created_at) FROM compute_tasks WHERE status='queued'").fetchone()[0]
            templates = connection.execute("SELECT COUNT(*) FROM compute_template_publications WHERE status='active'").fetchone()[0]
        return {"states": {row["status"]: row["amount"] for row in rows}, "oldest_queued_at": oldest, "templates": int(templates)}

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
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

    @staticmethod
    def _validate_inputs(schema: dict[str, dict[str, Any]], defaults: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
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

    @staticmethod
    def _content_digest(payload: dict[str, Any]) -> str:
        return digest(
            {
                "name": payload["name"],
                "algorithm": payload["algorithm"],
                "parameter_schema": payload["parameter_schema"],
                "default_parameters": payload["default_parameters"],
                "max_runtime_seconds": payload["max_runtime_seconds"],
                "max_attempts": payload["max_attempts"],
            }
        )

    @staticmethod
    def _require_template_code(repository: ComputeRepository, code: str) -> sqlite3.Row:
        template_code = repository.template_code_row(code)
        if template_code is None:
            raise NotFoundError("参数模板不存在")
        return template_code

    @staticmethod
    def _activate_due(repository: ComputeRepository, now: str, *, actor: str = "publication-scheduler") -> list[dict[str, Any]]:
        """把所有已到期的定时发布切换为 active。幂等，可在任意读路径惰性调用。"""
        activated: list[dict[str, Any]] = []
        for publication in repository.due_scheduled_publications(now):
            if repository.activate_publication(
                repository.connection,
                template_code_id=publication["template_code_id"],
                version=publication["version"],
                now=now,
            ):
                activated.append(
                    {
                        "template_code_id": publication["template_code_id"],
                        "version": publication["version"],
                        "effective_at": publication["effective_at"],
                        "activated_by": actor,
                    }
                )
        return activated

    def _activate_due_for(self, repository: ComputeRepository, template_code_id: int, now: str) -> None:
        self._activate_due(repository, now)

    @staticmethod
    def _template_version_view(repository: ComputeRepository, version: dict[str, Any]) -> dict[str, Any]:
        publication = repository.publication_for_version(version["template_code_id"], version["version"])
        view = dict(version)
        view["publication"] = dict(publication) if publication is not None else None
        return view

    @staticmethod
    def _build_diff(before: sqlite3.Row, after: sqlite3.Row) -> dict[str, Any]:
        changes: list[dict[str, Any]] = []
        for field in SCALAR_TEMPLATE_FIELDS:
            if before[field] != after[field]:
                changes.append({"field": field, "from": before[field], "to": after[field]})
        before_schema = json.loads(before["parameter_schema_json"])
        after_schema = json.loads(after["parameter_schema_json"])
        for name in sorted(set(before_schema) | set(after_schema)):
            if name not in before_schema:
                changes.append({"field": f"parameter_schema.{name}", "change": "added", "to": after_schema[name]})
            elif name not in after_schema:
                changes.append({"field": f"parameter_schema.{name}", "change": "removed", "from": before_schema[name]})
            elif before_schema[name] != after_schema[name]:
                changes.append({"field": f"parameter_schema.{name}", "change": "modified", "from": before_schema[name], "to": after_schema[name]})
        before_defaults = json.loads(before["default_parameters_json"])
        after_defaults = json.loads(after["default_parameters_json"])
        for name in sorted(set(before_defaults) | set(after_defaults)):
            if name not in before_defaults:
                changes.append({"field": f"default_parameters.{name}", "change": "added", "to": after_defaults[name]})
            elif name not in after_defaults:
                changes.append({"field": f"default_parameters.{name}", "change": "removed", "from": before_defaults[name]})
            elif before_defaults[name] != after_defaults[name]:
                changes.append({"field": f"default_parameters.{name}", "change": "modified", "from": before_defaults[name], "to": after_defaults[name]})
        return {
            "code": before["code"],
            "from_version": before["version"],
            "to_version": after["version"],
            "identical": before["content_digest"] == after["content_digest"],
            "changes": changes,
        }
