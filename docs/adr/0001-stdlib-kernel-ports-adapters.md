# ADR 0001：内核只用标准库，外部能力走端口/适配器

- 状态：已采纳（v0.2）
- 关联：RFC §3、§13、§16

## 决定
`wakecore/kernel` 只依赖 Python 3.12 标准库；数据库、时钟、密钥、数据源、工具、模型都通过
`wakecore/kernel/ports` 中的协议注入，实现在 `wakecore/adapters`。`tests/unit/test_architecture.py`
检查内核不导入适配器或第三方包。

## 理由
- 不变量（K-01…K-12）要能在假时钟、假工具、故障注入下确定性地测试。
- PostgreSQL 驱动（psycopg）是生产适配器的可选依赖，不进内核。

## 代价
- SQLite 适配器只用于开发与逻辑测试，它串行化写入，**不能**证明并发正确性；并发门禁见 ADR 0003 与
  `docs/runbooks/postgres.md`。
