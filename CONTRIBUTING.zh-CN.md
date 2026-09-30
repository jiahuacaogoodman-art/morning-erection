# 参与 WakeCore 开发

[English](CONTRIBUTING.md)

感谢参与。WakeCore 的价值全在它的保证上（一个副作用最多发生一次、一次审批只覆盖一份载荷、
回放绝不触碰外部世界），所以评审时先看改动可能破坏什么，再看它增加了什么。

## 环境

需要 [uv](https://docs.astral.sh/uv/) 与 Python 3.12+。

```bash
git clone https://github.com/jiahuacaogoodman-art/morning-erection && cd morning-erection
uv sync --all-packages --all-extras --group dev     # 整个 workspace 共用一个 .venv
uv run playwright install chromium                  # 只有 tests/ui 和 sidecar 需要
uv run wakecore demo                                # 离线演示：假时钟、无模型、无外部发送
```

目录：

| 路径 | 内容 |
|---|---|
| `packages/wakecore/src/wakecore` | 内核（PyPI 名 `morning-erection`），**只用标准库** |
| `packages/wakecore-ui-runtime/src/wakecore_ui_runtime` | 浏览器 sidecar（`morning-erection-ui-runtime`）：Playwright、Computer Use 循环 |
| `packages/wakecore/src/wakecore/protocol` | 共享契约：JSON Schema、校验器、OpenAPI 生成器 |
| `spec/` | 生成的 OpenAPI 文档，不要手改 |
| `tests/` | 全部测试（不进 wheel） |
| `examples/` | 插件示例包、任务示例 |
| `docs/` | 协议规范、extractor 参考、ADR、运维手册 |

## 测试

| 命令 | 覆盖 | 需要 |
|---|---|---|
| `uv run pytest -q --ignore=tests/ui` | 单元、契约、安全、恢复（含随机故障注入）、回放 | 无 |
| `WAKECORE_PG_DSN=postgresql://… uv run pytest -q tests/integration_postgres` | PostgreSQL 并发门禁（租约、执行槽、竞争下的 send-once） | 一次性数据库 |
| `WAKECORE_TEST_BACKEND=postgres WAKECORE_PG_DSN=… uv run pytest -q --ignore=tests/ui` | 所有 harness 测试改跑在 PG 上 | 同上 |
| `uv run pytest -q tests/ui` | 真 Chromium + sidecar 进程 + 模拟门户 + 模拟 OpenAI | Playwright + Chromium |
| `WAKECORE_REAL=1 uv run pytest -q -s tests/real` | 公网测试站点，以及（有 key 时）真实 Computer Use 模型 | 网络，可选 key |
| `WAKECORE_REAL=1 WAKECORE_REAL_DESKTOP=1 uv run pytest -q -s tests/real_desktop` | 真实 macOS 计算器 + 真实 MCP 桥接，以及（有 key 时）真实模型 | macOS、Node、桥接、辅助功能权限 |
| `uv run ruff check` | 代码检查 | — |
| `uv run python scripts/gen_openapi.py --check` | 提交的 OpenAPI 文档与路由表一致 | — |

本地一次性 PostgreSQL 见 [docs/runbooks/postgres.md](docs/runbooks/postgres.md)。
`tests/real` 和 `tests/real_desktop` 永不进 CI；它用的 key 放在 `.secrets/openai.key`（已被 git 忽略，`chmod 600`），只有 sidecar 进程读取。

提 PR 前至少跑第一条、lint 和 OpenAPI 检查。改了 sidecar 要跑 `tests/ui`；改了存储、租约或执行器要跑 PG 门禁。

## 不可违反的约束

能用测试保证的都有测试（`tests/unit/test_architecture.py`、`tests/security`、`tests/recovery`）。
削弱其中任何一条的 PR 都不会合并，无论功能多有用。

1. 不重写 `ObservationPort`、`ActionPort`、租约、调度器、审批、outbox、`UNKNOWN`/对账状态机；只扩展，要改先写 ADR。
2. 浏览器、模型、SDK 都是适配器或 sidecar；内核不直接依赖 Playwright、OpenAI/Anthropic SDK、Chrome，数据库驱动只在 `adapters/` 里。
3. 高频"插眼"走确定性读取（不调模型）。
4. Computer Use 只用于复杂、陌生或需要交互的网页操作。
5. 密码、Cookie、2FA Token 不进入模型上下文，也不存入 WakeCore 业务表。
6. Planner 只能看到当前任务真正获得授权的工具。
7. 每个浏览器动作都绑定允许访问的 origin / `resource_scope`。
8. `data_egress`、载荷、resource scope 共同绑定到动作 digest 和审批。
9. 所有提交、删除、上传执行后都要做结果验证。
10. 回放只重算历史记录，绝不重新操作网页或产生副作用。

另外：

- 内核分层 `domain` ← `ports` ← `kernel` ← `adapters` ← `app`；`protocol`、`testing`、`plugins.py` 是尽量小的公共面，架构测试会检查。
- 新的工具或数据源必须通过 `wakecore.testing.conformance`（见 [examples/custom_connector](examples/custom_connector)）。
- 同一主版本内协议只做增量：新字段一律可选，不改变已有字段或状态的含义；同步改 JSON Schema、
  重新生成 `spec/`（`uv run python scripts/gen_openapi.py`）和 [docs/spec/ui-runtime-protocol.md](docs/spec/ui-runtime-protocol.md)。
- 存储变更要带编号的 SQL 迁移，并保持表目录测试通过。
- 文档要诚实：写清楚验证过什么、在什么环境验证的。"未验证"是可以接受的答案。

## 提交与签名（DCO）

我们用 [Developer Certificate of Origin](https://developercertificate.org/) 代替 CLA，每个提交都要签名：

```bash
git commit -s -m "sidecar: refuse act when origins is empty"
```

这会加上 `Signed-off-by: 你的名字 <you@example.com>`，表示你有权以本项目的 MIT 许可提交这段改动。
提交保持聚焦，正文说明"为什么"。用户可感知的改动在 `CHANGELOG.md` 的 *Unreleased* 下加一行。

## 报告问题与安全漏洞

请用 issue 表单。**不要贴凭据、Cookie、API key、会话文件或已登录页面的截图。**
安全问题走私密报告，见 [SECURITY.md](SECURITY.md)。

参与即表示同意遵守[行为准则](CODE_OF_CONDUCT.md)。
