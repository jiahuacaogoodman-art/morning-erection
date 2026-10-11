/* Timeline — de-boxed dialogue flow.
   Tool calls are inline rows (icon · label · state), no card borders.
   Expanded body uses a thin left-rule for visual containment, not a card.
   data-testid="tool-row" and data-state are preserved for existing tests.
*/
import { useEffect, useState } from 'react'
import type {ReactNode} from 'react'
import { AlertCircle, ArrowUpRight, Check, ChevronDown, ChevronRight, ChevronUp, Eye, LogIn, RotateCw, Terminal } from 'lucide-react'
import type { DialogueMessage, Draft } from './types'
import { MorningMark } from './Brand'
import { CopyButton, Markdown } from './Markdown'
import type {WorkTarget} from './task-workspace'
/* ── diff renderer ── */
function DiffLine({ line }: { line: string }) {
  if (line.startsWith('+')) return <span className="tl-diff-add">{line}</span>
  if (line.startsWith('-')) return <span className="tl-diff-del">{line}</span>
  if (line.startsWith('@@')) return <span className="tl-diff-hunk">{line}</span>
  return <span className="tl-diff-ctx">{line}</span>
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split('\n')
  const adds = lines.filter(l => l.startsWith('+')).length
  const dels = lines.filter(l => l.startsWith('-')).length
  return (
    <div className="tl-diff">
      <div className="tl-diff-stats" aria-label={`${adds} 行新增，${dels} 行删除`}>
        <span className="tl-diff-stat-add">+{adds}</span>
        <span className="tl-diff-stat-del">-{dels}</span>
      </div>
      <pre className="tl-diff-body">
        {lines.map((line, i) => <DiffLine key={i} line={line} />)}
      </pre>
    </div>
  )
}

/* ── single tool row ── */
function ToolRow({ message,onOpenResource,action }: { message: DialogueMessage;onOpenResource?:(target:WorkTarget)=>void;action?:ReactNode }) {
  const [open, setOpen] = useState(false)
  const working = message.state === 'working'
  const failed  = message.state === 'error'

  const Icon = working ? RotateCw : failed ? AlertCircle : Check
  const args=(message.arguments||{}) as Record<string,unknown>
  const stateLabel = working ? '进行中' : failed ? '未成功' : message.terminal_id&&args.operation==='start'?'已启动':'已完成'
  const resource:WorkTarget|undefined=message.tool==='morning_computer'?{kind:'computer',id:message.id,device:typeof args.device_id==='string'?args.device_id:'local',app:typeof args.app==='string'?args.app:undefined,display:typeof args.display==='number'?args.display:undefined}:message.terminal_id?{kind:'terminal',id:message.terminal_id}:message.preview_id?{kind:'preview',id:message.preview_id}:undefined
  const hasBody = !!(message.diff || message.output || message.arguments || message.images?.length)

  return (
    <div className="tl-tool-row" data-testid="tool-row" data-state={message.state}>
      <button
        className="tl-tool-pill"
        onClick={() => hasBody && setOpen(o => !o)}
        aria-expanded={hasBody ? open : undefined}
        aria-label={`${message.text}，${stateLabel}`}
        disabled={!hasBody}
      >
        <Icon
          size={12}
          className={`tl-tool-icon tl-tool-icon--${working ? 'working' : failed ? 'error' : 'done'}`}
          aria-hidden="true"
        />
        <span className="tl-tool-label">{message.text}</span>
        <span className="tl-tool-state">{stateLabel}</span>
        {hasBody && (
          <span className="tl-tool-chevron" aria-hidden="true">
            {open ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
          </span>
        )}
      </button>
      {resource&&onOpenResource&&<button type="button" className="tl-resource-link" onClick={()=>onOpenResource(resource)}><ArrowUpRight size={11}/>{resource.kind==='computer'?'查看操作':resource.kind==='terminal'?'查看终端':'查看网页'}</button>}
      {action}

      {open && hasBody && (
        <div className="tl-tool-body">
          {message.images?.map((image,index)=><img key={index} className="tool-observation-image" src={`data:${image.mimeType};base64,${image.data}`} alt={`工具观察截图 ${index+1}`}/>)}
          {message.arguments != null && <details className="tl-tool-arguments"><summary>调用参数</summary><pre>{JSON.stringify(message.arguments, null, 2)}</pre></details>}
          {message.diff
            ? <DiffView diff={message.diff} />
            : message.output
              ? <pre className="tl-tool-output">{message.output}</pre>
              : null
          }
        </div>
      )}
    </div>
  )
}

/* ── proposal card ──
   The agent's understanding of a request is a *proposal*: it has inspected the page and
   built a watch, but nothing runs until the person confirms it. Until now no component read
   `message.draft`, so a proposal could never be accepted and a watch could never start.
   This is that confirmation, and it is the only thing that calls
   POST /pulse/watches/<id>/start. */
function ProposalCard({ message, onStart, started }: { message: DialogueMessage; onStart?: (draft: Draft) => Promise<void>; started?: boolean }) {
  const draft = message.draft
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(!!started)
  useEffect(() => {if (started) setDone(true)}, [started])
  if (!draft) return null
  const start = async () => {
    if (!onStart || busy || done) return
    setBusy(true); setError('')
    try {await onStart(draft); setDone(true)}
    catch (e) {setError(e instanceof Error ? e.message : '没有开始，请再试一次')}
    finally {setBusy(false)}
  }
  return (
    <div className="tl-proposal" data-testid="proposal">
      <div className="tl-proposal-kicker"><Eye size={15}/>驻守计划</div>
      <p className="tl-proposal-title">{draft.wait_summary || draft.condition_label}</p>
      <dl className="tl-proposal-facts">
        <div><dt>任务</dt><dd>{draft.label}</dd></div>
        {draft.source&&<div><dt>来源</dt><dd>{draft.source.target || '显示器 '+draft.source.display}</dd></div>}
        <div><dt>条件</dt><dd>{draft.condition_label}</dd></div>
        {draft.action && <div><dt>准备的操作</dt><dd>{draft.action.goal}</dd></div>}
      </dl>
      {draft.url && <a className="tl-agent-link" href={draft.url} target="_blank" rel="noreferrer">查看页面<ArrowUpRight size={13}/></a>}
      {draft.already_met && <p className="tl-proposal-note">当前观察已满足条件，启动后会先重新确认。</p>}
      {!!draft.data_egress?.length && <p className="tl-proposal-note">判断时会把相关观察发送到已选模型服务：{draft.data_egress.filter(item=>item!=='model:local-watch-plan').map(item=>item.replace(/^model:[^:]+:/,'')).join('、') || '本地处理'}。</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="tl-proposal-actions">
        <button type="button" className="primary-button" disabled={busy || done || !onStart} onClick={() => void start()}>
          {done ? <><Check size={14}/>已开始驻守</> : busy ? '正在开始…' : '开始驻守'}
        </button>
      </div>
    </div>
  )
}

/* ── agent text bubble ── */
function AgentBubble({ message, retry }: { message: DialogueMessage; retry?: () => void }) {
  const failed = message.type === 'error'
  if (message.type === 'context_summary') return <details className="tl-context-summary"><summary>Pi 已整理上下文 · 查看摘要</summary><Markdown text={message.text}/></details>
  return (
    <div className={`tl-agent-row${failed ? ' tl-agent-error' : ''}`}>
      <span className="tl-agent-icon" aria-hidden="true"><MorningMark size={34}/></span>
      <div className="tl-agent-content">
        <div className="tl-message-meta"><strong>Morning</strong><time dateTime={message.at}>{new Date(message.at).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit'})}</time></div>
        {message.thinking && <details className="tl-thinking"><summary>思考过程</summary><Markdown text={message.thinking}/></details>}
        {failed ? <div className="tl-error-content"><div className="tl-error-title"><AlertCircle size={16}/>这次未完成</div><p>{message.text}</p>
          {retry && <button className="text-button" onClick={retry}><RotateCw size={13}/>编辑后重试</button>}</div> : <Markdown text={message.text}/>}
        {message.url && (
          <a className="tl-agent-link" href={message.url} target="_blank" rel="noreferrer">
            {(() => { try { return new URL(message.url).hostname } catch { return message.url } })()}
          </a>
        )}
        {!failed && message.text && <div className="tl-message-actions"><CopyButton text={message.text}/></div>}
      </div>
    </div>
  )
}

/* ── user bubble ── */
function UserBubble({ message }: { message: DialogueMessage }) {
  return (
    <div className="tl-user-row">
      <div className="tl-user-bubble">{message.images?.map((image, index) => <img className="tl-attachment" key={index} src={`data:${image.mimeType};base64,${image.data}`} alt={`消息附件 ${index + 1}`}/>)}{message.text}
        {message.expanded_prompt && <details className="tl-expanded-prompt"><summary>查看 Skill 内容</summary><pre>{message.expanded_prompt}</pre></details>}</div>
    </div>
  )
}

/* ── public component ── */
interface TimelineProps {
  messages: DialogueMessage[]
  onStartWatch?: (draft: Draft) => Promise<void>
  startedTasks?: Set<string>
  onRetry?: (text: string) => void
  onLogin?: (url: string, finish: boolean) => Promise<unknown>
  onOpenResource?: (target:WorkTarget)=>void
  renderToolAction?: (message:DialogueMessage)=>ReactNode
}

function ToolGroup({messages,onOpenResource,renderToolAction}: {messages: DialogueMessage[];onOpenResource?:(target:WorkTarget)=>void;renderToolAction?:(message:DialogueMessage)=>ReactNode}) {
  const working = messages.some(m => m.state === 'working')
  const failed = messages.filter(m => m.state === 'error').length
  const actions=messages.map(message=>renderToolAction?.(message)),hasAction=actions.some(Boolean)
  const [open, setOpen] = useState(working||hasAction)
  useEffect(() => {if (working||hasAction) setOpen(true)}, [working,hasAction])
  return <div className="tl-tool-group"><button className="tl-tool-group-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
    <Terminal size={15}/><span>{working ? '正在执行' : '执行记录'}<small>{messages.length} 个步骤{failed > 0 ? ` · ${failed} 步未成功` : working ? '' : ' · 已完成'}</small></span>
    {open ? <ChevronDown size={14}/> : <ChevronRight size={14}/>}</button>
    {open && <div className="tl-tool-group-body">{messages.map((message,index) => <ToolRow key={message.id} message={message} onOpenResource={onOpenResource} action={actions[index]}/>)}</div>}
  </div>
}

function LoginAction({url, login}: {url: string; login: (url: string, finish: boolean) => Promise<unknown>}) {
  const [opened, setOpened] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <div className="tl-login"><button className="secondary-button" disabled={busy} onClick={() => {
    setBusy(true); setError(''); void login(url, opened).then(() => setOpened(!opened)).catch(e => setError(e.message || '登录窗口暂时无法打开')).finally(() => setBusy(false))
  }}><LogIn size={15}/>{busy ? '正在处理…' : opened ? '我已登录，关闭窗口' : '打开登录窗口'}</button>{error && <p className="inline-error" role="alert">{error}</p>}</div>
}

export function Timeline({ messages, onStartWatch, startedTasks, onRetry, onLogin,onOpenResource,renderToolAction }: TimelineProps) {
  if (messages.length === 0) return null

  const groups: DialogueMessage[][] = []
  for (const message of messages.filter(m => !m.rendered_by_stream)) {
    const previous = groups.at(-1)
    if (message.role === 'tool' && previous?.[0].role === 'tool') previous.push(message)
    else groups.push([message])
  }
  let userText = ''
  return <div className="tl-root" role="log" aria-label="对话时间线" aria-live="polite">
    {groups.map(group => {
      const message = group[0]
      if (message.role === 'tool') return <ToolGroup key={message.id} messages={group} onOpenResource={onOpenResource} renderToolAction={renderToolAction}/>
      if (message.role === 'user') {userText = message.text; return <UserBubble key={message.id} message={message}/>}
      const retryText = userText
      return <div key={message.id} className="tl-agent-block">
        <AgentBubble message={message} retry={onRetry && retryText ? () => onRetry(retryText) : undefined}/>
        {message.draft && <ProposalCard message={message} onStart={onStartWatch} started={startedTasks?.has(message.draft.task_id)}/>}
        {message.type === 'login' && message.url && onLogin && <LoginAction url={message.url} login={onLogin}/>}
      </div>
    })}
  </div>
}
