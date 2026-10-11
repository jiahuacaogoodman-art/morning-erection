/* StatusBar — animated-orb dynamic island that replaces the old .island system.
   Collapsed: one pill line (orb · summary text · chevron), height < 60px.
   Expanded: approval card or watch detail — same logic as the old Pulse component,
   new visual shell (no card borders, no dark backgrounds).
   data-testid="pulse" and data-state are preserved for existing tests.
*/
import { useEffect, useRef, useState } from 'react'
import { ArrowUpRight, Check, ChevronDown, ChevronUp, Globe, LogIn, Pause, Play, RotateCw, Settings2, ShieldCheck } from 'lucide-react'
import type { Approval, Watch } from './types'

const stateNames: Record<string, string> = {
  watching: '驻守中', working: '正在处理', approval: '等待批准',
  deferred: '等待时段', attention: '需要确认', reconnecting: '正在重连',
  connecting: '正在连接', paused: '已暂停', completed: '已完成',
  cancelled: '已结束', expired: '已到期', draft: '等待开始',
}

function age(at: string | null, now: number) {
  if (!at) return '等待首次确认'
  const s = Math.max(0, Math.floor((now - Date.parse(at)) / 1000))
  if (s < 5) return '刚刚'
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

function host(url: string) { try { return new URL(url).hostname } catch { return '网页' } }
function value(v: unknown): string {
  return v === true ? '已出现' : v === false ? '未出现' : v == null ? '—' : String(v)
}
function current(watch: Watch) { return value(Object.values(watch.records)[0]?.[watch.field]) }

interface StatusBarProps {
  watch: Watch | null
  now: number
  connectionError: string
  notice: { id: string; text: string } | null
  onSettings: () => void
  onRestart: () => void
  mutate: (path: string, body: unknown) => Promise<unknown>
  onNoticeClose: () => void
  /** User-chosen hue for the status orb; null follows the state's own colour. */
  orbHue?: number | null
  agentWorking?: boolean
  onReconnect?: () => void
  computerNotice?: {state:string; text:string; open:()=>void}
}

export function StatusBar({ watch, now, connectionError, notice, onSettings, onRestart, mutate, onNoticeClose, orbHue, agentWorking, onReconnect, computerNotice }: StatusBarProps) {
  const [open, setOpen] = useState(false)
  const [hover, setHover] = useState(false)
  const [busy, setBusy] = useState(false)
  const [login, setLogin] = useState(false)
  const barRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {if (!barRef.current?.contains(event.target as Node)) setOpen(false)}
    const escape = (event: KeyboardEvent) => {if (event.key === 'Escape') setOpen(false)}
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape)
    return () => {document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape)}
  }, [open])

  useEffect(() => { setOpen(false); setLogin(false) }, [watch?.task_id])
  useEffect(() => {
    if (watch?.session_state === 'human_login' || watch?.session_state === 'opening') setLogin(true)
    else if (watch?.session_state === 'closed' || watch?.session_state === 'login_window_failed') setLogin(false)
  }, [watch?.session_state])

  const computer = connectionError ? undefined : computerNotice
  const rawState = connectionError ? 'offline' : computer?.state || watch?.state || (agentWorking ? 'working' : 'empty')
  const urgent = ['approval', 'attention', 'offline'].includes(rawState)
  const transient = notice && !['completed', 'cancelled', 'expired', 'paused', 'reconnecting', 'deferred'].includes(rawState)
  const expanded = open || urgent || !!(watch && (transient || rawState === 'working'))
  const approval = watch?.pending_approvals[0] as Approval | undefined
  const expired = approval && Date.parse(approval.expires_at) <= now

  const act = async (path: string, body: unknown) => {
    setBusy(true)
    try { return await mutate(path, body) } catch { return undefined } finally { setBusy(false) }
  }

  // Build the collapsed one-line summary
  let summary = connectionError ? '连接中断' : stateNames[rawState] || '暂无驻守'
  if (watch && rawState === 'watching') summary = `${hover || open ? watch.label : '驻守中'} · ${watch.field_label} ${current(watch)}`
  if (watch && rawState === 'approval') summary = `${watch.label} · 等待批准`
  if (watch && rawState === 'working') summary = `${watch.label} · 正在处理…`
  if (watch && hover && rawState === 'watching') summary += ` · ${age(watch.last_confirmed_at, now)}`
  if (computer) summary = `电脑 · ${stateNames[computer.state] || '正在处理'}`

  const liveText = urgent
    ? `${summary}。${watch?.attention || approval?.payload.goal || connectionError}`
    : notice ? `检测到变化：${notice.text}` : ''

  return (
    <div className="status-bar-anchor">
      <div
        ref={barRef}
        className={`status-bar${expanded ? ' expanded' : ''}`}
        data-testid="pulse"
        data-state={rawState}
      >
        {/* ── collapsed pill ── */}
        <button
          className="status-bar-pill"
          onClick={() => { setOpen(!open); if (notice) onNoticeClose() }}
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
          onFocus={() => setHover(true)}
          onBlur={() => setHover(false)}
          aria-label={`Pulse：${summary}，点击查看详情`}
          aria-expanded={expanded}
          aria-controls={expanded ? 'status-bar-body' : undefined}
        >
          <span className="pulse-dot" style={orbHue == null ? undefined : {backgroundColor: `hsl(${orbHue} 50% 42%)`}}/>
          <span className="pulse-word">Pulse</span><span className="pulse-divider"/>
          <span className="status-bar-summary">{summary}</span>
          {(
            <span className="status-bar-chevron" aria-hidden="true">
              {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            </span>
          )}
        </button>

        {/* ── expanded body ── */}
        {expanded && computer && <div id="status-bar-body" className="status-bar-body" role="region" aria-label="电脑状态"><div className="sb-title">{computer.state==='approval'?'电脑操作等待你确认':'电脑操作需要查看'}</div><p className="sb-copy">{computer.text}</p><div className="sb-actions"><button onClick={()=>{computer.open();setOpen(false)}}>查看操作<ArrowUpRight size={13}/></button></div></div>}
        {expanded && !computer && !watch && <div id="status-bar-body" className="status-bar-body" role="region" aria-label="Pulse 详情">
          <div className="sb-title">{connectionError ? '本地服务连接中断' : agentWorking ? '正在处理你的消息' : '与外界保持连接'}</div>
          <p className="sb-copy">{connectionError ? '当前显示的是已保存的记录。恢复连接后，状态会自动更新。' : agentWorking ? '执行进度会出现在对话中。' : '需要持续关注变化时，直接在对话中告诉 Morning。驻守状态会集中出现在这里。'}</p>
          {connectionError && <div className="sb-actions"><button onClick={onReconnect}><RotateCw size={13}/>重新连接</button></div>}
        </div>}
        {expanded && !computer && watch && (
          <div id="status-bar-body" className="status-bar-body" role="region" aria-label="驻守详情">
            {rawState === 'approval' && approval ? (
              <>
                <div className="sb-title">{watch.condition_met ? `${watch.label}的等待条件已满足` : `${watch.label}有操作待确认`}</div>
                <p className="sb-copy">{approval.payload.goal || 'Agent 已准备好下一步操作'}</p>
                <a className="sb-link" href={watch.url || '#watch/'+watch.task_id} target={watch.url ? '_blank' : undefined} rel="noreferrer">
                  {host(watch.url)} <ArrowUpRight size={11} />
                </a>
                <details className="sb-detail">
                  <summary>查看这次操作</summary>
                  <div className="sb-receipt">
                    <span>目标：{approval.payload.start_url || watch.url}</span>
                    {!!approval.payload.verify && <span>完成后会重新读取页面，验证结果。</span>}
                    {approval.payload.verify?.conditions.map(c => (
                      <span key={c.field + c.op}>
                        确认：{c.field} {({ eq: '等于', gt: '大于', gte: '不小于', lt: '小于', lte: '不大于', contains: '包含', truthy: '存在', falsy: '不存在' } as Record<string, string>)[c.op] || c.op}{c.value === undefined ? '' : ` ${value(c.value)}`}
                      </span>
                    ))}
                    <code title="本次审批绑定的内容摘要">{approval.payload_digest.slice(0, 30)}…</code>
                  </div>
                </details>
                {expired && <p className="sb-inline-error">这次审批已到期，请重新准备操作。</p>}
                <div className="sb-actions">
                  <button
                    className="sb-approve"
                    disabled={busy || !!expired}
                    onClick={() => void act(`/approvals/${approval.approval_id}/approve`, { payload_digest: approval.payload_digest })}
                  >
                    <Check size={14} />{busy ? '处理中…' : '批准这次操作'}
                  </button>
                  <button
                    className="sb-reject"
                    disabled={busy}
                    onClick={() => void act(`/approvals/${approval.approval_id}/reject`, { reason: 'user_rejected_from_pulse' })}
                  >
                    拒绝
                  </button>
                </div>
                <div className="sb-foot"><ShieldCheck size={11} />审批只允许上面这一次操作</div>
              </>
            ) : rawState === 'attention' || rawState === 'offline' ? (
              <>
                <div className="sb-title">{watch.label}</div>
                <p className="sb-copy">
                  {connectionError ? '暂时无法连接后台。页面上的数值是上次确认的记录。' : watch.attention}
                </p>
                <div className="sb-last-known">上次确认：{watch.field_label} {current(watch)} · {age(watch.last_confirmed_at, now)}</div>
                {!connectionError && (
                  <div className="sb-actions">
                    {watch.source.health === 'AUTH_REQUIRED' && watch.url ? (
                      <>
                        <button
                          disabled={busy}
                          onClick={() => void act('/pulse/login', { task_id: watch.task_id, url: watch.url, finish: login }).then(r => { if (r) setLogin(!login) })}
                        >
                          <LogIn size={13} />{login ? '我已登录，关闭窗口' : '打开登录窗口'}
                        </button>
                        <button disabled={busy || login} onClick={() => void act(`/tasks/${watch.task_id}/check`, {})}>
                          <RotateCw size={13} />重新确认
                        </button>
                      </>
                    ) : watch.approval_expired ? (
                      <button onClick={onRestart}><RotateCw size={13} />重新确认驻守计划</button>
                    ) : ['SCHEMA_INVALID', 'DEGRADED'].includes(watch.source.health) ? (
                      <button disabled={busy} onClick={onSettings}><Settings2 size={13} />重新选择内容</button>
                    ) : (
                      <a className="sb-link" href={watch.url || '#watch/'+watch.task_id} target={watch.url ? '_blank' : undefined} rel="noreferrer">
                        查看来源，核实结果 <ArrowUpRight size={12} />
                      </a>
                    )}
                  </div>
                )}
              </>
            ) : transient && !open ? (
              <div className="sb-notice">
                <span className="sb-title">检测到变化</span>
                <p>{watch.label}：{notice?.text}</p>
                <span className="sb-caption">
                  {watch.configuration.spec.authority.write_capabilities.length ? '正在唤醒 Agent…' : '变化已记录'}
                </span>
              </div>
            ) : rawState === 'working' && !open ? (
              <>
                <div className="sb-title">Agent 已醒来</div>
                <p className="sb-copy">正在处理「{watch.label}」的变化</p>
                <span className="sb-work-trail"><i /><i /><i /></span>
              </>
            ) : (
              <>
                <div className="sb-task-heading">
                  <Globe size={13} /><span>{watch.label}</span><span className="sb-kind">{(watch.kind || 'web').toUpperCase()}</span>
                </div>
                {watch.state === 'deferred' && <p className="sb-copy">{watch.attention}</p>}
                <dl className="sb-facts">
                  <div><dt>当前</dt><dd>{watch.stale ? '上次记录 · ' : ''}{watch.field_label} {current(watch)}</dd></div>
                  <div><dt>等待</dt><dd>{watch.condition_label}</dd></div>
                  <div><dt>来源</dt><dd>{watch.url ? <a href={watch.url || '#watch/'+watch.task_id} target={watch.url ? '_blank' : undefined} rel="noreferrer">{host(watch.url)}<ArrowUpRight size={11} /></a> : <a href={'#watch/'+watch.task_id}>查看驻守来源<ArrowUpRight size={11}/></a>}</dd></div>
                  <div>
                    <dt>连接</dt>
                    <dd>
                      {watch.session_state === 'human_login' ? '等待你完成登录'
                        : watch.state === 'reconnecting' ? '正在重试'
                        : watch.source.health === 'HEALTHY' ? '读取正常'
                        : watch.attention || stateNames[watch.state]}
                    </dd>
                  </div>
                  <div>
                    <dt>上次确认</dt>
                    <dd>{age(watch.last_confirmed_at, now)}{watch.stale && <span className="sb-stale">历史记录</span>}</dd>
                  </div>
                </dl>
                {watch.approval_expired && <p className="sb-inline-error">有一项操作审批已到期，尚未执行。</p>}
                <div className="sb-actions">
                  <button disabled={busy || watch.lifecycle !== 'ACTIVE'} onClick={() => void act(`/tasks/${watch.task_id}/check`, {})}>
                    <RotateCw size={12} />立即确认
                  </button>
                  <button
                    disabled={busy || !['ACTIVE', 'PAUSED'].includes(watch.lifecycle)}
                    onClick={() => void act(`/tasks/${watch.task_id}/${watch.lifecycle === 'PAUSED' ? 'resume' : 'pause'}`, { expected_version: watch.version })}
                  >
                    {watch.lifecycle === 'PAUSED' ? <Play size={12} /> : <Pause size={12} />}
                    {watch.lifecycle === 'PAUSED' ? '继续' : '暂停'}
                  </button>
                  <button onClick={onSettings} aria-label="驻守设置"><Settings2 size={12} />设置</button>
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Live region for screen readers */}
      <div className="sr-only" aria-live={urgent ? 'assertive' : 'polite'}>{liveText}</div>
    </div>
  )
}
