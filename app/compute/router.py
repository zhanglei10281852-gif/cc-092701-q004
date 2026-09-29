from __future__ import annotations

from fastapi import APIRouter, Query

from app.compute.schemas import (
    BatchOperation,
    CancelRequest,
    DraftContent,
    PriorityRequest,
    PublishRequest,
    QuotaSet,
    RetryRequest,
    TaskClaim,
    TaskFailure,
    TaskResult,
    TaskSubmit,
    TemplateCreate,
)
from app.compute.service import ComputeOperationsService

router = APIRouter(prefix="/api/compute", tags=["科学计算任务运营"])


def service() -> ComputeOperationsService:
    return ComputeOperationsService()


# ----------------------------------------------------------------------
# 模板：当前有效版本
# ----------------------------------------------------------------------
@router.get("/templates")
def list_templates():
    return {"items": service().list_templates()}


@router.get("/templates/{code}")
def get_template(code: str):
    return service().get_template(code)


@router.post("/templates", status_code=201)
def create_template(payload: TemplateCreate, actor: str = Query(..., min_length=1)):
    return service().create_template(payload.model_dump(), actor)


# ----------------------------------------------------------------------
# 模板：草稿（可反复编辑，不影响查询与新任务）
# ----------------------------------------------------------------------
@router.get("/templates/{code}/draft")
def get_draft(code: str):
    return service().get_draft(code)


@router.put("/templates/{code}/draft")
def save_draft(code: str, payload: DraftContent, actor: str = Query(..., min_length=1)):
    return service().save_draft(code, payload.model_dump(), actor)


# ----------------------------------------------------------------------
# 模板：发布（立即或定时）与版本管理
# ----------------------------------------------------------------------
@router.post("/templates/{code}/publish", status_code=201)
def publish_template(code: str, payload: PublishRequest, actor: str = Query(..., min_length=1)):
    return service().publish_template(code, actor, payload.effective_at)


@router.post("/templates/{code}/cancel-schedule")
def cancel_scheduled_publication(code: str, actor: str = Query(..., min_length=1)):
    return service().cancel_scheduled_publication(code, actor)


@router.post("/templates/activate-due")
def activate_due_publications(actor: str = Query(default="publication-scheduler", min_length=1)):
    return service().activate_due_publications(actor)


@router.get("/templates/{code}/versions")
def list_versions(code: str):
    return service().list_versions(code)


@router.get("/templates/{code}/versions/{version}")
def get_version(code: str, version: int):
    return service().get_version(code, version)


@router.get("/templates/{code}/diff")
def diff_versions(
    code: str,
    from_version: int = Query(..., ge=1),
    to_version: int = Query(..., ge=1),
):
    return service().diff_versions(code, from_version, to_version)


# ----------------------------------------------------------------------
# 配额与任务
# ----------------------------------------------------------------------
@router.put("/quotas")
def set_quota(payload: QuotaSet, actor: str = Query(..., min_length=1)):
    return service().set_quota(payload.model_dump(), actor)


@router.post("/tasks", status_code=202)
def submit_task(payload: TaskSubmit):
    return service().submit(payload.model_dump())


@router.get("/tasks")
def list_tasks(status: str | None = None, project_code: str | None = None, requested_by: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_tasks(status=status, project_code=project_code, requested_by=requested_by, limit=limit)}


@router.get("/task-details/{task_id}")
def get_task(task_id: int):
    return service().get_task(task_id)


@router.post("/tasks/{task_id}/replay-validation")
def replay_task_validation(task_id: int):
    return service().replay_task_validation(task_id)


@router.post("/tasks/claim")
def claim_task(payload: TaskClaim):
    return {"task": service().claim(payload.worker_id, payload.capabilities, payload.lease_seconds)}


@router.post("/tasks/{task_id}/heartbeat")
def heartbeat(task_id: int, payload: TaskClaim):
    return service().heartbeat(task_id, payload.worker_id, payload.lease_seconds)


@router.post("/tasks/{task_id}/complete")
def complete_task(task_id: int, payload: TaskResult):
    return service().complete(task_id, payload.worker_id, payload.result, payload.metrics)


@router.post("/tasks/{task_id}/fail")
def fail_task(task_id: int, payload: TaskFailure):
    return service().fail(task_id, payload.worker_id, payload.error_code, payload.message, payload.retryable)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int, payload: CancelRequest):
    return service().cancel(task_id, payload.actor, payload.reason)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, payload: RetryRequest):
    return service().retry(task_id, payload.actor, payload.reason, payload.priority)


@router.post("/tasks/{task_id}/priority")
def set_priority(task_id: int, payload: PriorityRequest):
    return service().set_priority(task_id, payload.actor, payload.reason, payload.priority)


@router.post("/tasks/batch")
def batch_operation(payload: BatchOperation):
    return service().batch_operation(payload.model_dump())


@router.post("/recovery/expired-leases")
def recover_expired(actor: str = Query(default="recovery-worker", min_length=1)):
    return service().recover_expired(actor)


@router.get("/summary")
def summary():
    return service().summary()
