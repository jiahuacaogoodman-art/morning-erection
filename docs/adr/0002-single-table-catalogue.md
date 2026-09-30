# ADR 0002：唯一的表目录生成所有物理 DDL

- 状态：已采纳（v0.2）
- 关联：RFC §9.4、§11

## 决定
`wakecore/kernel/ports/tables.py` 是表、主键、唯一键、外键、索引的唯一声明。SQLite 与 PostgreSQL
的 DDL 都由 `SqlBuilder` 从它生成；`packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql` 也是生成物
（`python -m wakecore.adapters.postgres.migration`），测试会在文件与目录不一致时失败，
并在真实 PostgreSQL 上比较"执行 SQL 文件"与"运行时 migrate()"得到的 catalog 完全一致。

## 租户键
每个主键、唯一键、外键都以 `tenant_id` 开头，唯一例外是 `source_bindings.source_ref`：
接入地址 `POST /v1/ingress/{source_ref}` 只凭它定位绑定，因此它必须全局唯一，并由数据库约束保证
（不仅是注册代码的检查）。`tests/contract/test_migration_file.py` 固定了这一例外。

## 迁移策略（v0.2 的限制）
目前只有 0001 初始迁移，`migrate()` 使用 `CREATE … IF NOT EXISTS`，不处理列变更。
以后的结构变更必须新增编号迁移文件，不能修改 0001。
