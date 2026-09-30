# Runbook：PostgreSQL 部署与 M2 并发门禁

## 1. 角色要求（RFC §9.4）
应用角色**不得**是超级用户，也不得有 `BYPASSRLS`。建议迁移角色与应用角色分开：

```sql
CREATE ROLE wk_migrator LOGIN PASSWORD '…';
CREATE ROLE wk_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD '…';
CREATE DATABASE wakecore OWNER wk_migrator;
\c wakecore
GRANT CONNECT ON DATABASE wakecore TO wk_app;
GRANT USAGE ON SCHEMA public TO wk_app;
-- 以 wk_migrator 执行 0001_init.sql（见第 2 节）之后：
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO wk_app;
```

门禁测试 `test_pg_role_is_not_superuser_or_bypassrls` 会检查连接角色；只有在明确知道自己在做什么时
才用 `WAKECORE_PG_ALLOW_SUPERUSER=1` 跳过。

## 2. 建表
SQL 迁移文件随内核包发布，在仓库里的位置是 `packages/wakecore/src/wakecore/adapters/postgres/sql/`：
```bash
psql "$MIGRATOR_DSN" -f packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql            # 新库
psql "$MIGRATOR_DSN" -f packages/wakecore/src/wakecore/adapters/postgres/sql/0002_v03_action_scope.sql # 只用于从 v0.2 建的库升级
```
从 v0.2 升级前先处理完进行中的动作：v0.2 写入的动作 digest 不含 scope/egress，升级后派发会以 `digest_mismatch` 拒绝（fail closed）。
已安装的包里可以这样找到文件：
`python -c "import importlib.resources as r; print(r.files('wakecore.adapters.postgres') / 'sql')"`。

或者 `wakecore --db "$DSN" init-db`（运行时从同一表目录建表，结果与 SQL 文件一致，有测试保证）。
SQL 文件是生成物，改表请改 `wakecore/kernel/ports/tables.py` 后重新生成：
```bash
uv run python -m wakecore.adapters.postgres.migration > packages/wakecore/src/wakecore/adapters/postgres/sql/0001_init.sql
```

## 3. 运行 M2 门禁（本地，一次性数据库）
需要一个**可随意丢弃**的数据库；测试在其中为每个用例建独立 schema 并在结束时删除。

```bash
uv sync --all-packages --all-extras --group dev   # dev 依赖组里已有 psycopg 和 pgserver（仅用于本地嵌入式 PG）
```
部署时只需要 `pip install "morning-erection[postgres]"`。

没有现成 PostgreSQL 时可用 pgserver 起一个嵌入式实例（本仓库的验证就是这样做的，PG 16.2）：
```python
import pgserver
s = pgserver.get_server("/tmp/wk-pg", cleanup_mode=None)
s.psql("CREATE ROLE wk_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;")
s.psql("CREATE DATABASE wk_test;")
s.psql("GRANT CONNECT, CREATE ON DATABASE wk_test TO wk_app;")
```
（测试需要 `CREATE` 权限来建临时 schema；生产应用角色不需要。）

```bash
export WAKECORE_PG_DSN='postgresql://wk_app@/wk_test?host=/private/tmp/wk-pg'
uv run pytest -q tests/integration_postgres                       # M2 门禁
WAKECORE_TEST_BACKEND=postgres uv run pytest -q --ignore=tests/ui  # 全部 harness 测试改跑在 PG 上
```
未设置 `WAKECORE_PG_DSN` 时这些测试会被**跳过**（显示为 skipped），不代表通过。
用 pgserver 时，它的 Python 包自带服务端，启动一次后数据保存在 `/tmp/wk-pg`：
`uv run python -c "import pgserver; pgserver.get_server('/tmp/wk-pg', cleanup_mode=None)"`。
CI 里用的是 `postgres:16` 服务容器，见 `.github/workflows/ci.yml` 的 `postgres` 任务。

## 4. 门禁覆盖什么、不覆盖什么
覆盖：8 个并发 worker 处理 6 个任务每个动作只执行一次；同一事件 8 路并发接入只接受 1 次；
预算最后额度并发只扣一次；并发幂等创建返回同一结果；并发审批只发送一次；source_ref 抢注只成功一次；
SQL 迁移文件与运行时迁移一致。

不覆盖：多机/网络分区、主从切换、长时间运行、容量与延迟（未做任何容量测试）。
