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

## 模板版本化

评分参数模板不再原地修改，而是采用“草稿 → 发布 → 版本”的可追溯模型：

- 新建模板只产生一个**草稿**（`POST /api/compute/templates`），草稿可用 `PUT /api/compute/templates/{code}/draft` 反复编辑，期间不出现在有效模板列表中，也不能被新任务引用。
- `POST /api/compute/templates/{code}/publish` 发布草稿：不带 `effective_at` 表示立即生效；传入未来时间表示**定时启用**，到期前查询与新任务仍按旧版本执行。到期切换由显式调度 `POST /api/compute/templates/activate-due` 触发，也会在任意读/写路径上惰性、幂等地完成。可用 `POST /api/compute/templates/{code}/cancel-schedule` 取消尚未生效的安排。
- 每次发布生成一个不可变版本（`compute_template_versions`），新版本生效时旧发布置为 `superseded`，但版本行保留。
- 任务在提交时把当时完整的校验输入冻结进 `validation_snapshot_json`（参数模式、默认值、合并后的提交参数、运行/重试限额、算法与版本号）。服务重启后，`POST /api/compute/tasks/{id}/replay-validation` 与任务详情始终按快照复核，不读取当前版本。
- 被任务引用的旧版本受外键 `ON DELETE RESTRICT` 保护，不可删除，只可通过 `GET /api/compute/templates/{code}/versions/{version}` 回放。
- 管理员可用 `GET /api/compute/templates/{code}/diff?from_version=&to_version=` 查看任意两版差异。
- 同一模板任意时刻至多有一个 active 与一个 scheduled 发布（部分唯一索引 + `BEGIN IMMEDIATE` 事务）；并发或重复发布会得到 409，失败事务整体回滚，不改变当前有效版本，草稿仍是草稿。

旧版单行 `compute_templates` 结构会在 `init-db`/启动时自动迁移：旧模板成为已发布 v1，旧任务回填校验快照，结果与人工干预记录原样保留。


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
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
