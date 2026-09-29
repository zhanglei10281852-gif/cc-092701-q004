from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.core.errors import ConflictError, ValidationError
from app.database import close_connection, get_connection, init_db


TEMPLATE = {
    "code": "solver-a",
    "name": "方程求解模板",
    "algorithm": "solver-a",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "tolerance": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {"tolerance": 0.001},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}

V2_CONTENT = {
    "name": "方程求解模板（修订）",
    "algorithm": "solver-a-v2",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 500},
        "tolerance": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "precision": {"type": "string", "required": False, "choices": ["single", "double"]},
    },
    "default_parameters": {"tolerance": 0.01, "precision": "double"},
    "max_runtime_seconds": 120,
    "max_attempts": 4,
}


def submit_payload(key: str, *, user: str = "researcher-1", iterations: int = 100, extra: dict | None = None) -> dict:
    parameters = {"iterations": iterations, "mode": "accurate"}
    if extra:
        parameters.update(extra)
    return {
        "template_code": "solver-a",
        "project_code": "project-a",
        "requested_by": user,
        "parameters": parameters,
        "priority": 50,
        "idempotency_key": key,
    }


def create_template(client) -> None:
    response = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert response.status_code == 201, response.text


def create_draft(client, **payload) -> dict:
    response = client.post("/api/compute/templates/solver-a/versions?actor=administrator", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def update_draft(client, version: int, content: dict | None = None) -> dict:
    response = client.put(f"/api/compute/templates/solver-a/versions/{version}?actor=administrator", json=content or V2_CONTENT)
    assert response.status_code == 200, response.text
    return response.json()


def publish(client, version: int, **payload) -> dict:
    response = client.post(f"/api/compute/templates/solver-a/versions/{version}/publish?actor=administrator", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def versions(client) -> list[dict]:
    response = client.get("/api/compute/templates/solver-a/versions")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_create_template_publishes_version_one(client):
    create_template(client)
    items = versions(client)
    assert len(items) == 1
    assert items[0]["version"] == 1
    assert items[0]["status"] == "published"
    assert items[0]["state"] == "effective"
    assert items[0]["published_by"] == "administrator"
    templates = client.get("/api/compute/templates").json()["items"]
    assert templates[0]["current_version"] == 1
    assert templates[0]["max_runtime_seconds"] == 300


def test_draft_edit_publish_flow_and_task_snapshot(client):
    create_template(client)
    old_task = client.post("/api/compute/tasks", json=submit_payload("version-flow-0001", iterations=800))
    assert old_task.status_code == 202

    draft = create_draft(client)
    assert draft["version"] == 2 and draft["state"] == "draft" and draft["base_version"] == 1

    # 草稿可以反复编辑
    first_edit = dict(V2_CONTENT, max_runtime_seconds=180)
    assert update_draft(client, 2, first_edit)["max_runtime_seconds"] == 180
    assert update_draft(client, 2)["max_runtime_seconds"] == 120

    # 草稿未发布前，新任务仍按 v1 规则校验
    still_old_rules = client.post("/api/compute/tasks", json=submit_payload("version-flow-0002", iterations=800))
    assert still_old_rules.status_code == 202

    published = publish(client, 2)
    assert published["state"] == "effective" and published["published_by"] == "administrator"

    # 发布后新任务按 v2 规则校验：iterations=800 超出新上限，mode 参数已被移除
    rejected = client.post("/api/compute/tasks", json=submit_payload("version-flow-0003", iterations=800))
    assert rejected.status_code == 422
    accepted = client.post("/api/compute/tasks", json=submit_payload("version-flow-0004", iterations=400, extra={"mode": None}))
    assert accepted.status_code == 422  # mode 已不在 v2 schema 中
    new_task = client.post(
        "/api/compute/tasks",
        json={**submit_payload("version-flow-0005", iterations=400), "parameters": {"iterations": 400}},
    )
    assert new_task.status_code == 202
    new_detail = client.get(f"/api/compute/task-details/{new_task.json()['id']}").json()
    assert new_detail["template_version"] == 2
    assert new_detail["max_attempts"] == 4
    assert new_detail["validation_snapshot"]["parameter_schema"]["iterations"]["maximum"] == 500
    assert new_detail["validation_snapshot"]["supplied_parameters"] == {"iterations": 400}

    # 旧任务仍冻结在 v1 的校验输入上
    old_detail = client.get(f"/api/compute/task-details/{old_task.json()['id']}").json()
    assert old_detail["template_version"] == 1
    assert old_detail["validation_snapshot"]["parameter_schema"]["iterations"]["maximum"] == 10000
    assert old_detail["validation_snapshot"]["supplied_parameters"] == {"iterations": 800, "mode": "accurate"}

    replay = client.post(f"/api/compute/tasks/{old_task.json()['id']}/revalidate")
    assert replay.status_code == 200
    body = replay.json()
    assert body["consistent"] and body["valid"]
    assert body["template_version"] == 1 and body["source"] == "snapshot"
    assert body["version_record_matches_snapshot"]


def test_scheduled_publish_does_not_affect_queries_until_effective(client):
    from app.database import init_db as ensure_db

    ensure_db()
    clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=UTC))
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    draft = service.create_draft("solver-a", {}, "administrator")
    service.update_draft("solver-a", draft["version"], V2_CONTENT, "administrator")
    scheduled = service.publish_version(
        "solver-a", draft["version"],
        {"effective_at": clock.now() + timedelta(days=1), "expected_effective_version": None, "force": False},
        "administrator",
    )
    assert scheduled["state"] == "scheduled"

    # 生效时间未到：列表、提交校验、版本状态都仍是 v1
    templates = service.list_templates()
    assert templates[0]["current_version"] == 1 and templates[0]["max_runtime_seconds"] == 300
    task = service.submit(submit_payload("scheduled-000001", iterations=800))
    assert task["template_version"] == 1
    states = {item["version"]: item["state"] for item in service.list_versions("solver-a")}
    assert states == {1: "effective", 2: "scheduled"}

    # 时钟越过生效时间后，查询自动切换到 v2，无需任何后台任务
    clock.advance(days=2)
    templates = service.list_templates()
    assert templates[0]["current_version"] == 2 and templates[0]["max_runtime_seconds"] == 120
    with pytest.raises(ValidationError):
        service.submit(submit_payload("scheduled-000002", iterations=800))
    states = {item["version"]: item["state"] for item in service.list_versions("solver-a")}
    assert states == {1: "superseded", 2: "effective"}


def test_scheduled_publish_rejects_past_effective_time(client):
    create_template(client)
    draft = create_draft(client)
    response = client.post(
        f"/api/compute/templates/solver-a/versions/{draft['version']}/publish?actor=administrator",
        json={"effective_at": "2020-01-01T00:00:00+00:00"},
    )
    assert response.status_code == 422
    assert versions(client)[1]["state"] == "draft"


def test_publish_conflict_on_stale_base_and_expected_version(client):
    create_template(client)
    first = create_draft(client)  # v2 草稿，基线 v1
    second = create_draft(client)  # v3 草稿，同样基于 v1（并行编辑）
    assert first["base_version"] == second["base_version"] == 1
    publish(client, first["version"])  # v2 先生效

    # v3 的基线已经过期：默认拒绝发布并给出冲突上下文
    conflict = client.post(
        f"/api/compute/templates/solver-a/versions/{second['version']}/publish?actor=administrator", json={}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"] == {"base_version": 1, "current_effective_version": 2}

    # 预期有效版本不符同样拒绝
    mismatch = client.post(
        f"/api/compute/templates/solver-a/versions/{second['version']}/publish?actor=administrator",
        json={"expected_effective_version": 1},
    )
    assert mismatch.status_code == 409

    # 即使预期版本判断正确，过期基线仍需显式 force 确认
    still_stale = client.post(
        f"/api/compute/templates/solver-a/versions/{second['version']}/publish?actor=administrator",
        json={"expected_effective_version": 2},
    )
    assert still_stale.status_code == 409

    # 管理员审查差异后强制发布
    forced = publish(client, second["version"], expected_effective_version=2, force=True)
    assert forced["state"] == "effective"

    # 同一版本只能发布一次，重复发布视为冲突
    again = client.post(
        f"/api/compute/templates/solver-a/versions/{second['version']}/publish?actor=administrator", json={}
    )
    assert again.status_code == 409


def test_publish_expected_effective_version_guard(client):
    create_template(client)
    draft = create_draft(client)  # 基线 v1，当前有效版本也是 v1
    mismatch = client.post(
        f"/api/compute/templates/solver-a/versions/{draft['version']}/publish?actor=administrator",
        json={"expected_effective_version": 7},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["context"] == {"expected": 7, "current": 1}
    assert publish(client, draft["version"], expected_effective_version=1)["state"] == "effective"


def test_failed_publish_does_not_pollute_effective_version(client):
    create_template(client)
    draft = create_draft(client)
    update_draft(client, draft["version"])  # v2：上限收紧到 500

    failed = client.post(
        f"/api/compute/templates/solver-a/versions/{draft['version']}/publish?actor=administrator",
        json={"expected_effective_version": 99},
    )
    assert failed.status_code == 409

    # 当前有效版本未被污染：查询、提交校验、版本状态全部保持 v1
    templates = client.get("/api/compute/templates").json()["items"]
    assert templates[0]["current_version"] == 1
    still_valid = client.post("/api/compute/tasks", json=submit_payload("failed-publish-0001", iterations=800))
    assert still_valid.status_code == 202
    states = {item["version"]: item["state"] for item in versions(client)}
    assert states == {1: "effective", 2: "draft"}

    # 草稿仍可修正后正常发布
    publish(client, draft["version"])
    rejected = client.post("/api/compute/tasks", json=submit_payload("failed-publish-0002", iterations=800))
    assert rejected.status_code == 422


def test_published_versions_cannot_be_deleted_but_drafts_can(client):
    create_template(client)
    task = client.post("/api/compute/tasks", json=submit_payload("delete-guard-0001"))
    assert task.status_code == 202
    publish(client, create_draft(client)["version"])  # v2 生效，v1 仍被任务引用

    for version in (1, 2):
        response = client.delete(f"/api/compute/templates/solver-a/versions/{version}?actor=administrator")
        assert response.status_code == 409

    draft = create_draft(client)
    deleted = client.delete(f"/api/compute/templates/solver-a/versions/{draft['version']}?actor=administrator")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert [item["version"] for item in versions(client)] == [1, 2]

    # 被引用的旧版本仍可完整读取，用于回放
    old = client.get("/api/compute/templates/solver-a/versions/1")
    assert old.status_code == 200
    assert old.json()["state"] == "superseded" and old.json()["task_count"] == 1
    assert old.json()["parameter_schema"]["iterations"]["maximum"] == 10000


def test_published_version_content_is_immutable(client):
    create_template(client)
    response = client.put("/api/compute/templates/solver-a/versions/1?actor=administrator", json=V2_CONTENT)
    assert response.status_code == 409


def test_diff_between_any_two_versions(client):
    create_template(client)
    draft = create_draft(client)
    update_draft(client, draft["version"])
    publish(client, draft["version"])

    diff = client.get("/api/compute/templates/solver-a/diff?from_version=1&to_version=2")
    assert diff.status_code == 200
    body = diff.json()
    assert body["identical"] is False
    assert body["from"]["version"] == 1 and body["to"]["version"] == 2
    changes = {(change["field"], change["kind"]): change for change in body["changes"]}
    assert changes[("name", "modified")]["after"] == "方程求解模板（修订）"
    assert changes[("algorithm", "modified")]["after"] == "solver-a-v2"
    assert changes[("max_runtime_seconds", "modified")]["before"] == 300
    assert changes[("max_attempts", "modified")]["after"] == 4
    assert changes[("parameter_schema.iterations.maximum", "modified")]["before"] == 10000
    assert changes[("parameter_schema.iterations.maximum", "modified")]["after"] == 500
    assert changes[("parameter_schema.mode", "removed")]["before"]["required"] is True
    assert changes[("parameter_schema.precision", "added")]["after"]["choices"] == ["single", "double"]
    assert changes[("default_parameters.tolerance", "modified")]["after"] == 0.01
    assert changes[("default_parameters.precision", "added")]["after"] == "double"

    same = client.get("/api/compute/templates/solver-a/diff?from_version=1&to_version=1").json()
    assert same["identical"] is True and same["changes"] == []

    missing = client.get("/api/compute/templates/solver-a/diff?from_version=1&to_version=9")
    assert missing.status_code == 404


def test_historical_tasks_revalidate_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(tmp_path / "restart.db"))
    close_connection()
    try:
        init_db()
        clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=UTC))
        service = ComputeOperationsService(get_connection(), clock)
        service.create_template(TEMPLATE, "administrator")
        task = service.submit(submit_payload("restart-000001", iterations=800))

        # 发布与旧规则不兼容的新版本
        draft = service.create_draft("solver-a", {}, "administrator")
        service.update_draft("solver-a", draft["version"], V2_CONTENT, "administrator")
        service.publish_version("solver-a", draft["version"], {}, "administrator")

        # 模拟服务重启：关闭连接，用同一数据库文件重建服务
        close_connection()
        restarted = ComputeOperationsService(get_connection(), clock)

        replay = restarted.revalidate_task(task["id"])
        assert replay["source"] == "snapshot"
        assert replay["valid"] and replay["consistent"]
        assert replay["template_version"] == 1
        assert replay["normalized_parameters"] == {"iterations": 800, "mode": "accurate", "tolerance": 0.001}
        assert replay["version_record_matches_snapshot"]

        detail = restarted.get_task(task["id"])
        assert detail["validation_snapshot"]["template_version"] == 1

        # 新任务按新规则复核，旧任务回放不受其影响
        with pytest.raises(ValidationError):
            restarted.submit(submit_payload("restart-000002", iterations=800))
    finally:
        close_connection()


def test_revalidate_detects_tampered_parameters(client):
    create_template(client)
    task = client.post("/api/compute/tasks", json=submit_payload("tamper-000001")).json()
    from app.database import transaction

    with transaction(immediate=True) as connection:
        connection.execute("UPDATE compute_tasks SET parameters_json=? WHERE id=?", ('{"iterations": 5}', task["id"]))
    replay = client.post(f"/api/compute/tasks/{task['id']}/revalidate").json()
    assert replay["valid"] is True
    assert replay["consistent"] is False


def test_version_endpoints_not_found(client):
    create_template(client)
    assert client.get("/api/compute/templates/unknown-x/versions").status_code == 404
    assert client.get("/api/compute/templates/solver-a/versions/9").status_code == 404
    assert client.post("/api/compute/templates/unknown-x/versions?actor=administrator", json={}).status_code == 404
    assert client.post("/api/compute/tasks/9999/revalidate").status_code == 404


LEGACY_SCHEMA = """
CREATE TABLE compute_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL CHECK(max_runtime_seconds > 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
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
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
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
"""


def test_legacy_database_is_migrated_with_replayable_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(tmp_path / "legacy.db"))
    close_connection()
    try:
        # 模拟旧版本服务留下的表结构与历史任务
        connection = get_connection()
        connection.executescript(LEGACY_SCHEMA)
        connection.execute(
            "INSERT INTO compute_templates(code,name,algorithm,parameter_schema_json,default_parameters_json,max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at) VALUES('legacy-solver','遗留模板','legacy-solver','{\"iterations\": {\"type\": \"integer\", \"required\": true, \"minimum\": 1, \"maximum\": 10000}}','{}',300,2,1,'administrator','2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')"
        )
        connection.execute(
            "INSERT INTO compute_tasks(template_id,project_code,requested_by,parameters_json,parameter_digest,priority,idempotency_key,status,attempt_count,max_attempts,available_at,created_at,updated_at) VALUES(1,'project-a','researcher-1','{\"iterations\": 800}','legacy-digest',50,'legacy-key-000001','succeeded',1,2,'2026-09-02T00:00:00+00:00','2026-09-02T00:00:00+00:00','2026-09-02T00:00:00+00:00')"
        )

        # 新版本服务启动，执行幂等迁移
        init_db()
        init_db()  # 重复执行不应产生重复数据
        clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=UTC))
        service = ComputeOperationsService(get_connection(), clock)

        items = service.list_versions("legacy-solver")
        assert len(items) == 1
        assert items[0]["version"] == 1 and items[0]["state"] == "effective"
        assert items[0]["task_count"] == 1

        detail = service.get_task(1)
        assert detail["template_version"] == 1
        assert detail["validation_snapshot"]["parameter_schema"]["iterations"]["maximum"] == 10000
        assert detail["validation_snapshot"]["supplied_parameters"] == {"iterations": 800}

        replay = service.revalidate_task(1)
        assert replay["valid"] and replay["template_version"] == 1
        assert replay["normalized_parameters"] == {"iterations": 800}
        assert replay["version_record_matches_snapshot"]

        # 迁移后新任务走版本化流程
        task = service.submit(
            {
                "template_code": "legacy-solver",
                "project_code": "project-a",
                "requested_by": "researcher-1",
                "parameters": {"iterations": 5},
                "priority": 50,
                "idempotency_key": "legacy-key-000002",
            }
        )
        assert task["template_version"] == 1
    finally:
        close_connection()
