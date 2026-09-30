# ADR 0003：每任务执行槽 + 带 epoch 的租约；并发正确性以 PostgreSQL 为准

- 状态：已采纳（v0.2）
- 关联：RFC §10、§12、M2 门禁

## 决定
- 同一任务同一时刻只有一个步骤在执行（`execution_slots`），不同任务并行。
- 步骤与 outbox 条目通过租约认领；每次接管递增 `lease_epoch`，提交前 `verify_for_commit` 校验
  owner 与 epoch，过期的执行者无法提交（T07）。
- PostgreSQL 适配器使用 READ COMMITTED + 行锁（`FOR UPDATE`）+ `SKIP LOCKED` 认领；
  SQLite 用 `BEGIN IMMEDIATE` 串行化。

## 证据边界
- 逻辑正确性：SQLite 下的恢复/故障测试与随机故障测试。
- 并发正确性：只有 `tests/integration_postgres`（真实并发写者）算数。本仓库已在单机嵌入式
  PostgreSQL 16.2、非超级用户角色下跑通；这不是容量或多机测试。
