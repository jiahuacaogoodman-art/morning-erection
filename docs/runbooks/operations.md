# Runbook：日常运维与故障处理

## 进程
- `wakecore serve-api`：管理 API + 接入 API，只写数据库，不执行工作。令牌与密钥从
  `WAKECORE_SECRETS_FILE` 读取（见 `wakecore/app/cli/main.py` 顶部注释），不要放命令行参数。
- `wakecore worker`：调度 → 恢复（租约过期、对账、DRAINING 收尾）→ 步骤 → 派发。可以多开；
  并发安全依赖 PostgreSQL（SQLite 只适合单进程开发）。
- 进程随时可以被杀：租约过期后其他 worker 接管，过期执行者无法提交。

## 看任务
```bash
wakecore --db "$DSN" task show <task_id>
wakecore --db "$DSN" task timeline <task_id>
wakecore --db "$DSN" replay <task_id> -v     # 只读确定性回放，外部写入恒为 0
```
回放出现 mismatch 表示记录的证据/决策与当前 reducer 重算结果不一致（证据被改、版本变更等），
应先保留现场再排查，不要"修数据让它一致"。

## 动作卡在 UNKNOWN
含义：已经尝试派发，但不知道外部系统是否生效。内核**不会**自动重发。
1. worker 会周期性对账；支持对账的工具（站内信、邮件适配器）会自行收敛为 CONFIRMED 或 FAILED_NO_EFFECT。
2. 不支持对账或长期不确定时，人工去提供方核实，然后：
   `POST /v1/actions/{id}/resolve`，body `{"note": "已在邮件后台确认送达"}` → `RESOLVED_MANUALLY`。
   不要手工改库重发。

## 任务停在 DRAINING
目标已满足，但还有通知未确认。检查该任务的 notify 动作：是否有 UNKNOWN（见上）、是否有
FAILED_NO_EFFECT 但无 `#retryN` 后继且重试次数未用完（这是 v0.2 修过的缺陷，出现即为 bug，请保留数据库报告）。

## 隔离区（quarantine）
不支持的事件 schema 版本、同 effect_key 不同内容等会进入 `quarantine` 表并附原因。
它们不会被猜测处理；确认后由新的显式命令处理。

## 密钥轮换 / 撤销授权
- 撤销 grant：`POST /v1/grants/{grant_ref}/revoke`。已批准未派发的动作在派发前复核授权，会被拒绝。
- 数据源重新授权后：`POST /v1/bindings/{source_ref}/reauthorised`。
