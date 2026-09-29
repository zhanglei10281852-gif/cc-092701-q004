from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection, init_db


TEMPLATE_V1 = {
    "code": "solver-b",
    "name": "方程求解模板",
    "algorithm": "solver-b",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "tolerance": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {"tolerance": 0.001},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}

TEMPLATE_V2 = {
    **TEMPLATE_V1,
    "name": "方程求解模板（精调）",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 100},
        "tolerance": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "max_attempts": 3,
}


def payload(key: str, *, iterations: int = 500, user: str = "researcher-1") -> dict:
    return {
        "template_code": "solver-b",
        "project_code": "project-b",
        "requested_by": user,
        "parameters": {"iterations": iterations, "mode": "accurate"},
        "priority": 50,
        "idempotency_key": key,
    }


def make_service(clock: FrozenClock | None = None) -> ComputeOperationsService:
    return ComputeOperationsService(get_connection(), clock or FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC)))


@pytest.fixture()
def isolated_db(tmp_path: Path):
    previous = os.environ.get("TOWNSHIP_DATABASE_PATH")
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(tmp_path / "isolated.db")
    close_connection()
    init_db()
    try:
        yield
    finally:
        close_connection()
        if previous is None:
            os.environ.pop("TOWNSHIP_DATABASE_PATH", None)
        else:
            os.environ["TOWNSHIP_DATABASE_PATH"] = previous


def setup_published_v1(clock: FrozenClock | None = None) -> tuple[ComputeOperationsService, FrozenClock]:
    clock = clock or FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service = make_service(clock)
    service.create_template(TEMPLATE_V1, "administrator")
    service.publish_template("solver-b", "administrator", None)
    return service, clock


def test_unpublished_draft_is_invisible_and_not_referenceable(client):
    created = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE_V1)
    assert created.status_code == 201
    assert created.json()["status"] == "draft"
    assert client.get("/api/compute/templates").json()["items"] == []
    rejected = client.post("/api/compute/tasks", json=payload("draft-000001"))
    assert rejected.status_code == 404


def test_draft_can_be_repeatedly_edited_without_creating_versions(client):
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE_V1)
    changed = dict(TEMPLATE_V2)
    first = client.put("/api/compute/templates/solver-b/draft?actor=teacher-1", json=changed)
    assert first.status_code == 200 and first.json()["version"] == 1
    changed["name"] = "再次修改"
    second = client.put("/api/compute/templates/solver-b/draft?actor=teacher-1", json=changed)
    assert second.status_code == 200 and second.json()["version"] == 1
    assert second.json()["name"] == "再次修改"
    versions = client.get("/api/compute/templates/solver-b/versions").json()["items"]
    assert len(versions) == 1 and versions[0]["status"] == "draft"
    # 草稿改动不影响查询：仍无有效版本
    assert client.get("/api/compute/templates").json()["items"] == []


def test_each_publish_creates_immutable_version_and_new_tasks_pin_it(client):
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE_V1)
    client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    task_v1 = client.post("/api/compute/tasks", json=payload("pin-000001")).json()
    assert task_v1["template_version"] == 1

    client.put("/api/compute/templates/solver-b/draft?actor=teacher-1", json=TEMPLATE_V2)
    client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    task_v2 = client.post("/api/compute/tasks", json=payload("pin-000002", iterations=50)).json()
    assert task_v2["template_version"] == 2

    # 旧任务仍指向 v1，新任务按 v2 被拒绝（500 > 新上限 100）
    rejected = client.post("/api/compute/tasks", json=payload("pin-000003", iterations=500))
    assert rejected.status_code == 422
    details_old = client.get(f"/api/compute/task-details/{task_v1['id']}").json()
    details_new = client.get(f"/api/compute/task-details/{task_v2['id']}").json()
    assert details_old["template_version"] == 1
    assert details_new["template_version"] == 2


def test_task_freezes_complete_validation_snapshot(client):
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE_V1)
    client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    task = client.post("/api/compute/tasks", json=payload("snap-000001", iterations=500)).json()
    details = client.get(f"/api/compute/task-details/{task['id']}").json()
    snapshot = details["validation_snapshot"]
    assert snapshot["template_version"] == 1
    assert snapshot["parameter_schema"] == TEMPLATE_V1["parameter_schema"]
    assert snapshot["default_parameters"] == {"tolerance": 0.001}
    # 完整校验输入：提交参数与默认值合并后冻结
    assert snapshot["parameters"] == {"iterations": 500, "mode": "accurate", "tolerance": 0.001}
    assert snapshot["max_attempts"] == 2 and snapshot["algorithm"] == "solver-b"


def test_scheduled_publication_does_not_affect_queries_before_due_time(isolated_db):
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service, clock = setup_published_v1(clock)
    old_task = service.submit(payload("future-000001", iterations=500))
    assert old_task["template_version"] == 1

    service.save_draft("solver-b", TEMPLATE_V2, "teacher-1")
    due = clock.now() + timedelta(days=1)
    scheduled = service.publish_template("solver-b", "administrator", due)
    assert scheduled["publication"]["status"] == "scheduled"

    # 到期前：查询与新任务仍按 v1
    assert service.list_templates()[0]["active_version"] == 1
    assert service.get_template("solver-b")["version"] == 1
    before_due_task = service.submit(payload("future-000002", iterations=500))
    assert before_due_task["template_version"] == 1

    # 到期后（显式调度或任意读路径惰性激活）：v2 生效
    clock.advance(days=1, seconds=1)
    result = service.activate_due_publications()
    assert [item["version"] for item in result["activated"]] == [2]
    assert service.get_template("solver-b")["version"] == 2
    after_due_task = service.submit(payload("future-000003", iterations=50))
    assert after_due_task["template_version"] == 2
    with pytest.raises(Exception):
        service.submit(payload("future-000004", iterations=500))


def test_lazy_activation_happens_on_query_path(isolated_db):
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service, clock = setup_published_v1(clock)
    service.save_draft("solver-b", TEMPLATE_V2, "teacher-1")
    service.publish_template("solver-b", "administrator", clock.now() + timedelta(hours=2))
    clock.advance(hours=3)
    # 不调用显式调度接口，列表查询自身完成到期切换
    assert service.list_templates()[0]["active_version"] == 2


def test_history_tasks_replay_by_original_rules_after_restart(isolated_db):
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service, clock = setup_published_v1(clock)
    old_task = service.submit(payload("replay-000001", iterations=500))

    service.save_draft("solver-b", TEMPLATE_V2, "teacher-1")
    service.publish_template("solver-b", "administrator", None)

    # 模拟服务重启：关闭连接、重新初始化，再起一个服务实例
    close_connection()
    connection = get_connection()
    init_db()
    restarted = ComputeOperationsService(connection, clock)
    replay = restarted.replay_task_validation(old_task["id"])
    assert replay["valid"] is True
    assert replay["template_version"] == 1
    # 当前有效版本已是 v2，但旧任务快照复核依然通过（500 在 v2 下本应非法）
    assert restarted.get_template("solver-b")["version"] == 2
    details = restarted.get_task(old_task["id"])
    assert details["validation_snapshot"]["parameters"]["iterations"] == 500


def test_old_versions_remain_playable_and_cannot_be_deleted(isolated_db):
    service, _ = setup_published_v1()
    old_task = service.submit(payload("keep-000001", iterations=500))
    service.save_draft("solver-b", TEMPLATE_V2, "teacher-1")
    service.publish_template("solver-b", "administrator", None)

    versions = service.list_versions("solver-b")["items"]
    assert [item["version"] for item in versions] == [1, 2]
    v1 = service.get_version("solver-b", 1)
    assert v1["status"] == "published"
    assert service.replay_task_validation(old_task["id"])["valid"] is True

    # 存储层兜底：被任务引用的旧版本禁止删除
    with pytest.raises(sqlite3.IntegrityError):
        service.connection.execute("DELETE FROM compute_template_versions WHERE code='solver-b' AND version=1")
    service.connection.rollback()


def test_diff_between_any_two_versions(client):
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE_V1)
    client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    client.put("/api/compute/templates/solver-b/draft?actor=teacher-1", json=TEMPLATE_V2)
    client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    diff = client.get("/api/compute/templates/solver-b/diff?from_version=1&to_version=2").json()
    fields = {change["field"]: change for change in diff["changes"]}
    assert "name" in fields
    assert fields["parameter_schema.iterations"]["from"]["maximum"] == 10000
    assert fields["parameter_schema.iterations"]["to"]["maximum"] == 100
    assert fields["max_attempts"]["from"] == 2 and fields["max_attempts"]["to"] == 3
    same = client.get("/api/compute/templates/solver-b/diff?from_version=1&to_version=1")
    assert same.status_code == 422
    missing = client.get("/api/compute/templates/solver-b/diff?from_version=1&to_version=9")
    assert missing.status_code == 404


def test_simultaneous_scheduled_publish_conflicts_and_keeps_current_version(isolated_db):
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=UTC))
    service, clock = setup_published_v1(clock)
    # v2 安排在明天
    service.save_draft("solver-b", TEMPLATE_V2, "teacher-1")
    service.publish_template("solver-b", "administrator", clock.now() + timedelta(days=1))

    # v2 已占用待生效槽位后，再起 v3 草稿并尝试立即发布 → 冲突
    v3 = dict(TEMPLATE_V2)
    v3["name"] = "第三版"
    service.save_draft("solver-b", v3, "teacher-2")
    from app.core.errors import ConflictError
    with pytest.raises(ConflictError):
        service.publish_template("solver-b", "administrator", None)

    # 失败的发布没有污染当前有效版本：v1 仍有效，v3 仍是草稿
    assert service.get_template("solver-b")["version"] == 1
    draft = service.get_draft("solver-b")
    assert draft["version"] == 3 and draft["status"] == "draft"

    # 取消定时发布后 v3 可立即发布
    cancelled = service.cancel_scheduled_publication("solver-b", "administrator")
    assert cancelled["cancelled_version"] == 2
    published = service.publish_template("solver-b", "administrator", None)
    assert published["version"] == 3 and published["status"] == "published"
    assert service.get_template("solver-b")["version"] == 3


def test_failed_publish_rolls_back_draft_state(isolated_db):
    service, clock = setup_published_v1()
    service.save_draft("solver-b", TEMPLATE_V2, "teacher-1")
    service.publish_template("solver-b", "administrator", clock.now() + timedelta(hours=1))
    v3 = dict(TEMPLATE_V2)
    v3["max_attempts"] = 5
    service.save_draft("solver-b", v3, "teacher-2")
    from app.core.errors import ConflictError
    with pytest.raises(ConflictError):
        service.publish_template("solver-b", "administrator", None)
    # 回滚后 v3 行仍然存在且状态为 draft，发布时间为空
    row = service.repository.draft_template_version(service.repository.template_code_row("solver-b")["id"])
    assert row["version"] == 3 and row["status"] == "draft" and row["published_at"] is None


def test_publish_without_draft_conflicts(client):
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE_V1)
    client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    again = client.post("/api/compute/templates/solver-b/publish?actor=administrator", json={})
    assert again.status_code == 409


LEGACY_DDL = """
CREATE TABLE compute_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE compute_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES compute_templates(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    current_result_version INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE TABLE compute_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES compute_tasks(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    result_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(task_id, version)
);
CREATE TABLE compute_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES compute_tasks(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def test_legacy_database_migrates_and_history_still_replays(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(LEGACY_DDL)
    raw.execute(
        "INSERT INTO compute_templates(code,name,algorithm,version,parameter_schema_json,default_parameters_json,"
        "max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES(?,?,?,1,?,?,?,2,1,'admin','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
        (
            "solver-b", "方程求解模板", "solver-b",
            json.dumps(TEMPLATE_V1["parameter_schema"], ensure_ascii=False),
            json.dumps({"tolerance": 0.001}, ensure_ascii=False),
            300,
        ),
    )
    raw.execute(
        "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,"
        "idempotency_key,status,max_attempts,available_at,created_at,updated_at) "
        "VALUES(1,'project-b','researcher-1',?,?, 'legacy-000001','succeeded',2,'2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')",
        (
            json.dumps({"iterations": 500, "mode": "accurate", "tolerance": 0.001}, ensure_ascii=False, sort_keys=True),
            "legacy-digest",
        ),
    )
    raw.execute(
        "INSERT INTO compute_results(task_id,version,result_json,metrics_json,result_digest,created_by,created_at) "
        "VALUES(1,1,'{\"value\": 1}','{}','r','worker-1','2026-09-01T00:01:00+00:00')"
    )
    raw.commit()
    raw.close()

    close_connection()
    os.environ["TOWNSHIP_DATABASE_PATH"] = str(db_path)
    init_db()
    try:
        service = make_service()
        templates = service.list_templates()
        assert len(templates) == 1 and templates[0]["active_version"] == 1
        details = service.get_task(1)
        assert details["template_version"] == 1
        assert details["validation_snapshot"]["parameters"]["iterations"] == 500
        assert len(details["results"]) == 1 and details["results"][0]["result_json"] == '{"value": 1}'
        # 迁移幂等：再次初始化不报错、不重复
        init_db()
        assert len(service.list_versions("solver-b")["items"]) == 1
    finally:
        close_connection()
