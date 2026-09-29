from __future__ import annotations

import json
from typing import Any

from app.core.security import request_fingerprint

_MISSING = object()


def version_content(*, name: str, algorithm: str, parameter_schema: dict[str, Any], default_parameters: dict[str, Any], max_runtime_seconds: int, max_attempts: int) -> dict[str, Any]:
    """参与校验、摘要与版本对比的模板内容字段。"""
    return {
        "name": name,
        "algorithm": algorithm,
        "parameter_schema": parameter_schema,
        "default_parameters": default_parameters,
        "max_runtime_seconds": max_runtime_seconds,
        "max_attempts": max_attempts,
    }


def content_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return version_content(
        name=row["name"],
        algorithm=row["algorithm"],
        parameter_schema=json.loads(row["parameter_schema_json"]),
        default_parameters=json.loads(row["default_parameters_json"]),
        max_runtime_seconds=row["max_runtime_seconds"],
        max_attempts=row["max_attempts"],
    )


def content_digest(content: dict[str, Any]) -> str:
    return request_fingerprint(content)


def build_validation_snapshot(*, template_code: str, version_row: dict[str, Any], supplied_parameters: dict[str, Any], captured_at: str) -> dict[str, Any]:
    """任务创建时冻结的完整校验输入，供历史任务按原规则回放复核。"""
    return {
        "template_code": template_code,
        "template_version": version_row["version"],
        "template_version_id": version_row["id"],
        "name": version_row["name"],
        "algorithm": version_row["algorithm"],
        "parameter_schema": json.loads(version_row["parameter_schema_json"]),
        "default_parameters": json.loads(version_row["default_parameters_json"]),
        "max_runtime_seconds": version_row["max_runtime_seconds"],
        "max_attempts": version_row["max_attempts"],
        "content_digest": version_row["content_digest"],
        "effective_at": version_row["effective_at"],
        "published_at": version_row["published_at"],
        "supplied_parameters": supplied_parameters,
        "captured_at": captured_at,
    }


def version_state(*, status: str, effective_at: str | None, is_current: bool, now: str) -> str:
    if status == "draft":
        return "draft"
    if effective_at and effective_at > now:
        return "scheduled"
    return "effective" if is_current else "superseded"


def _record(changes: list[dict[str, Any]], field: str, before: Any, after: Any) -> None:
    if before is _MISSING and after is _MISSING:
        return
    if before is _MISSING:
        changes.append({"field": field, "kind": "added", "before": None, "after": after})
    elif after is _MISSING:
        changes.append({"field": field, "kind": "removed", "before": before, "after": None})
    elif before != after:
        changes.append({"field": field, "kind": "modified", "before": before, "after": after})


def _diff_mapping(changes: list[dict[str, Any]], prefix: str, before_map: dict[str, Any], after_map: dict[str, Any], *, nested: bool) -> None:
    for key in sorted(set(before_map) | set(after_map)):
        field = f"{prefix}.{key}"
        before_value = before_map.get(key, _MISSING)
        after_value = after_map.get(key, _MISSING)
        if nested and isinstance(before_value, dict) and isinstance(after_value, dict):
            for rule_key in sorted(set(before_value) | set(after_value)):
                _record(changes, f"{field}.{rule_key}", before_value.get(rule_key, _MISSING), after_value.get(rule_key, _MISSING))
        else:
            _record(changes, field, before_value, after_value)


def diff_content(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """对两个版本的内容字段做结构化对比，供管理员审查任意两版差异。"""
    changes: list[dict[str, Any]] = []
    for field in ("name", "algorithm", "max_runtime_seconds", "max_attempts"):
        _record(changes, field, before.get(field, _MISSING), after.get(field, _MISSING))
    _diff_mapping(changes, "parameter_schema", before.get("parameter_schema") or {}, after.get("parameter_schema") or {}, nested=True)
    _diff_mapping(changes, "default_parameters", before.get("default_parameters") or {}, after.get("default_parameters") or {}, nested=False)
    return changes
