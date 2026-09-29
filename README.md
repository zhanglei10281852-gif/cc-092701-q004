# 职业教学任务运营服务

这是一个面向职业院校教务团队、授课教师和课程管理员的 Python 后端服务，用于管理课程任务模板、学员提交、执行队列、教师工作者、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/compute-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

课程任务运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 模板版本与发布

评分参数的每次修订都是可追溯的版本操作，已经交给教师批改的任务不会被新规则悄然解释：

- 创建模板即发布第 1 版并立即生效；后续修订通过 `POST /api/compute/templates/{code}/versions` 从任一已发布版本派生草稿，草稿可用 `PUT .../versions/{version}` 反复编辑，只有 `POST .../versions/{version}/publish` 发布后的版本才能被新任务引用。
- 任务提交时会把当时完整的校验输入（参数模式、默认值、运行限制、原始提交参数、内容摘要）冻结在任务记录中，服务重启后仍可通过 `POST /api/compute/tasks/{task_id}/revalidate` 按原规则复核。
- 发布时可指定未来的 `effective_at` 定时启用；生效时间由查询时的时钟判定，未到期的版本不会提前影响模板查询与任务校验。
- 已发布版本不可删除（`DELETE` 仅允许草稿），仍被任务引用的旧版本始终可读取、可回放。
- 管理员可用 `GET /api/compute/templates/{code}/diff?from_version=&to_version=` 查看任意两版的结构化差异；并行草稿同时发布时，基线已过期的草稿会收到 409 冲突，审查差异后可基于最新版本重建草稿或显式 `force` 发布。
- 发布在单个事务中完成，内容复核或冲突检查失败会整体回滚，不会污染当前有效版本。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板版本、配额、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。模板版本的内容保存在 `compute_template_versions`，任务的校验快照保存在 `compute_tasks.validation_snapshot_json`，两者都随库文件持久化，升级时由幂等迁移自动补齐历史数据。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
