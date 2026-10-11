import { useEffect, useId, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { AlertCircle, ArrowDown, ArrowUp, Check, ChevronRight, Eye, FolderOpen, Menu, MessageSquare, Monitor, ListTodo, Laptop, Paperclip, RotateCw, Settings as SettingsIcon, SlidersHorizontal, Square, SquarePen, X } from 'lucide-react'
import { api, ApiError } from './api'
import { StatusBar } from './StatusBar'
import { Timeline } from './Timeline'
import { SettingsView, alertUser, useSettings } from './Settings'
import type { Change, Draft, PiImage, State, Watch } from './types'
import { MorningMark } from './Brand'
import { NativeInteraction, NativePanel, piCommand, piControl } from './NativePi'
import type {PiSession} from './NativePi'
import {ComputerApprovals, PlatformModules, PlatformView, usePlatform} from './Platform'
import type {PlatformPage} from './Platform'

function value(v: unknown): string {return v === true ? '已出现' : v === false ? '未出现' : v == null ? '尚未读到' : String(v)}
function changeText(changes: Change[], field: string) {
  return changes.map(c => `${value(c.old?.[field])} → ${value(c.new?.[field])}`).join('，')
}

function usePulse() {
  const [data, setData] = useState<State | null>(null)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [clockOffset, setClockOffset] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let stopped = false
    let connected = !!data
    const update = async () => {
      let active = false
      try {
        if (!connected) {await api('/bootstrap', undefined, controller.signal); connected = true}
        let next: State
        try {next = await api<State>('/pulse/state', undefined, controller.signal)}
        catch (e) {
          if (!(e instanceof ApiError && e.status === 401)) throw e
          await api('/bootstrap', undefined, controller.signal)
          next = await api<State>('/pulse/state', undefined, controller.signal)
        }
        if (!stopped) {setData(next); setClockOffset(Date.parse(next.server_time) - Date.now()); setError('')}
        active = !!next.agent_busy
      } catch (e) {
        if (!stopped) setError(e instanceof Error ? e.message : '暂时无法连接')
      } finally {
        if (!stopped) timer = setTimeout(update, document.hidden ? 10000 : active ? 350 : 1500)
      }
    }
    void update()
    const reconnect = () => setRevision(r => r + 1)
    window.addEventListener('online', reconnect)
    const visibility = () => {if (!document.hidden) reconnect()}
    document.addEventListener('visibilitychange', visibility)
    return () => {stopped = true; controller.abort(); clearTimeout(timer); window.removeEventListener('online', reconnect);
      document.removeEventListener('visibilitychange', visibility)}
    // Poll sequentially; the kernel worker remains active when this tab is hidden or closed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision])
  return {data, error, clockOffset, refresh: () => setRevision(r => r + 1)}
}

function Dialog({title, children, close, wide = false}: {title: string; children: ReactNode; close: () => void; wide?: boolean}) {
  const ref = useRef<HTMLDivElement>(null)
  const id = useId()
  const closeRef = useRef(close)
  closeRef.current = close
  useEffect(() => {
    const previous = document.activeElement as HTMLElement
    const focusable = () => Array.from(ref.current?.querySelectorAll<HTMLElement>('button,input,textarea,select,a[href],[tabindex="0"]') || [])
      .filter(el => !el.hasAttribute('disabled') && el.getClientRects().length > 0)
    focusable()[0]?.focus()
    const handle = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {event.preventDefault(); closeRef.current()}
      if (event.key === 'Tab') {
        const elements = focusable(), first = elements[0], last = elements[elements.length - 1]
        if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last?.focus()}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus()}
      }
    }
    document.addEventListener('keydown', handle)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {document.removeEventListener('keydown', handle); document.body.style.overflow = prevOverflow; previous?.focus()}
  }, [])
  return <div className="dialog-scrim" onMouseDown={e => {if (e.target === e.currentTarget) close()}}>
    <div ref={ref} className={`drawer ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={id}>
      <header className="drawer-header"><div><span className="eyebrow">PULSE</span><h2 id={id}>{title}</h2></div>
        <button className="icon-button" aria-label="关闭" onClick={close}><X size={18}/></button></header>{children}
    </div>
  </div>
}


function WatchSettings({watch, close, mutate}: {watch: Watch; close: () => void; mutate: (path: string, body: unknown) => Promise<unknown>}) {
  const [minimum, setMinimum] = useState(watch.configuration.polling.min_seconds)
  const [maximum, setMaximum] = useState(watch.configuration.polling.max_seconds)
  const [extractor, setExtractor] = useState(JSON.stringify(watch.configuration.extractor, null, 2))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const save = async (e: FormEvent) => {e.preventDefault(); setBusy(true); setError(''); try {
    await mutate(`/pulse/watches/${watch.task_id}/settings`, {spec_digest: watch.spec_digest, ...(!watch.kind || watch.kind === 'web' ? {extractor: JSON.parse(extractor)} : {}),
      polling: {...watch.configuration.polling, min_seconds: minimum, max_seconds: maximum, error_max_seconds: Math.max(maximum, watch.configuration.polling.error_max_seconds)}})
    close()
  } catch (e) {setError(e instanceof SyntaxError ? '读取配置不是有效的 JSON，请检查后再保存。' : e instanceof Error ? e.message : '保存未完成')} finally {setBusy(false)}}
  return <Dialog title={`${watch.label} · 设置`} close={close}><form className="creator-body" onSubmit={e => void save(e)}>
    <p className="support-copy">长期没有变化时放慢检查；出现变化后恢复较快的节奏。</p>
    <div className="condition-form"><label className="field-label">最短间隔 · 秒<input type="number" min="2" max="86400" required value={minimum} onChange={e => setMinimum(Number(e.target.value))}/></label>
      <label className="field-label">最长间隔 · 秒<input type="number" min={minimum} max="86400" required value={maximum} onChange={e => setMaximum(Number(e.target.value))}/></label></div>
    <p className="support-copy">当前约 {watch.interval_seconds} 秒一次。服务器限流时会等待更久。</p>
    {(!watch.kind || watch.kind === 'web') && <details className="advanced"><summary>页面读取配置</summary><textarea aria-label="页面读取配置 JSON" className="code-input" rows={15} value={extractor} onChange={e => setExtractor(e.target.value)}/>
      <p className="support-copy">修改后会建立新基准，原有记录仍保留。</p></details>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    <button className="primary-button full" disabled={busy || !['ACTIVE','PAUSED'].includes(watch.lifecycle)}>{busy ? '正在保存…' : '保存设置'}</button>
    <button className="danger-link" type="button" disabled={busy || !['ACTIVE','PAUSED'].includes(watch.lifecycle)} onClick={() => {setBusy(true); void mutate(`/pulse/watches/${watch.task_id}/end`, {reason: 'user_ended_watch', expected_version: watch.version}).then(close).finally(() => setBusy(false))}}>结束这个驻守</button>
  </form></Dialog>
}


export default function App() {
  const {data, error, clockOffset, refresh} = usePulse()
  const [selectedId, setSelectedId] = useState<string | null>(() => localStorage.getItem('pulse:selected'))
  const [settings, setSettings] = useState(false)
  const [view, setView] = useState<'chat' | 'settings' | PlatformPage>(() => {const page = location.hash.slice(1).split('/')[0]; return ['settings','computer','watch','tasks','devices'].includes(page) ? page as 'settings' | PlatformPage : 'chat'})
  const platform = usePlatform(!!data?.platform_available)
  useEffect(() => {const changed = () => {const page = location.hash.slice(1).split('/')[0];setView(['settings','computer','watch','tasks','devices'].includes(page) ? page as 'settings' | PlatformPage : 'chat')};window.addEventListener('hashchange',changed);return () => window.removeEventListener('hashchange',changed)}, [])
  useEffect(() => {if(location.hash.slice(1).split('/')[0] !== view) location.hash = view}, [view])
  useEffect(() => {const key = (e:KeyboardEvent) => {if((e.metaKey || e.ctrlKey) && /^[1-6]$/.test(e.key)){e.preventDefault();setView((['chat','computer','watch','tasks','devices','settings'] as const)[Number(e.key)-1]);setMobileNav(false)}};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key)}, [])
  const app = useSettings()
  const [mobileNav, setMobileNav] = useState(false)
  const [smallScreen, setSmallScreen] = useState(() => window.matchMedia('(max-width:720px)').matches)
  const [prompt, setPrompt] = useState('')
  const [now, setNow] = useState(Date.now())
  const [toast, setToast] = useState('')
  const [notice, setNotice] = useState<{id: string; task_id: string; text: string} | null>(null)
  const conversation = useRef<HTMLDivElement>(null)
  const composer = useRef<HTMLTextAreaElement>(null)
  const navigation = useRef<HTMLElement>(null)
  const [messageBusy, setMessageBusy] = useState(false)
  const [nativePanel, setNativePanel] = useState(false)
  const [sessions, setSessions] = useState<PiSession[]>([])
  const [queueMode, setQueueMode] = useState<'steer' | 'followUp'>('steer')
  const [images, setImages] = useState<PiImage[]>([])
  const attachment = useRef<HTMLInputElement>(null)
  const pi = data?.pi
  const handledEditor = useRef('')
  const handledNotice = useRef<string | number>('')
  const dialogue = data?.dialogue || []
  const agentWorking = messageBusy || !!data?.agent_busy
  const workspaceName = data?.workspace?.split(/[\\/]/).filter(Boolean).at(-1)
  const [following, setFollowing] = useState(true)
  const tasks = data?.items.filter(t => !['DRAFT','CANCELLED','EXPIRED'].includes(t.lifecycle)) || []
  const selected = tasks.find(t => t.task_id === selectedId) || tasks[0] || null
  const urgent = tasks.find(t => t.state === 'approval') || tasks.find(t => t.state === 'attention')
  const computerUrgent = platform.data?.computer.find(job=>job.state==='approval') || platform.data?.computer.find(job=>!job.dismissed&&['failed','unknown','expired'].includes(job.state))
  const pulseWatch = urgent || tasks.find(t => t.task_id === notice?.task_id) || selected
  useEffect(() => {if (pi) void piControl<PiSession[]>('sessions').then(setSessions).catch(() => {})}, [pi?.state?.sessionId, nativePanel])
  useEffect(() => {if (pi?.editor && pi.editor.id !== handledEditor.current) {handledEditor.current = pi.editor.id; setPrompt(pi.editor.text)}}, [pi?.editor?.id])
  useEffect(() => {const last = pi?.notices?.at(-1); if (last?.message && last.id !== handledNotice.current) {handledNotice.current = last.id; setToast(last.message)}}, [pi?.notices?.at(-1)?.id])
  useEffect(() => {document.title = pi?.title || 'Morning Erection · Agent'}, [pi?.title])
  useEffect(() => {if (pi?.info?.warning) setToast(pi.info.warning)}, [pi?.info?.warning])
  useEffect(() => {const media = window.matchMedia('(max-width:720px)'); const change = () => setSmallScreen(media.matches);
    media.addEventListener('change', change); return () => media.removeEventListener('change', change)}, [])
  useEffect(() => {const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer)}, [])
  useEffect(() => {
    if (!mobileNav || !smallScreen) return
    const controls = () => Array.from(navigation.current?.querySelectorAll<HTMLElement>('button,a[href]') || [])
    controls()[0]?.focus()
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {setMobileNav(false); requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('.mobile-menu')?.focus())}
      if (event.key === 'Tab') {
        const items = controls(), first = items[0], last = items.at(-1)
        if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last?.focus()}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus()}
      }
    }
    document.addEventListener('keydown', keyboard)
    return () => document.removeEventListener('keydown', keyboard)
  }, [mobileNav, smallScreen])
  useEffect(() => {if (selected) localStorage.setItem('pulse:selected', selected.task_id)}, [selected?.task_id])
  useEffect(() => {
    if (!data) return
    for (const task of data.items) {
      const latest = task.changes.at(-1)
      if (!latest) continue
      const key = `pulse:seen:${task.task_id}`
      if (localStorage.getItem(key) === latest.id) continue
      localStorage.setItem(key, latest.id)
      if (Date.now() + clockOffset - Date.parse(latest.at) < 60000 && task.state !== 'completed') {
        const text = changeText(latest.changes, task.field)
        setNotice({id: latest.id, task_id: task.task_id, text})
        alertUser(app.settings, 'change', `${task.label} 有变化`, text)
      }
    }
    for (const task of data.items) for (const approval of task.pending_approvals) {
      const key = `pulse:alerted:${approval.approval_id}`
      if (localStorage.getItem(key)) continue
      localStorage.setItem(key, '1')
      alertUser(app.settings, 'approval', `${task.label} 等待你批准`, approval.payload.goal || 'Agent 已准备好下一步操作')
    }
  }, [data])
  useEffect(() => {if (!notice) return; const timer = setTimeout(() => setNotice(null), 6500); return () => clearTimeout(timer)}, [notice?.id])
  useEffect(() => {if (!toast) return; const timer = setTimeout(() => setToast(''), 4500); return () => clearTimeout(timer)}, [toast])
  useEffect(() => {if (following && conversation.current) conversation.current.scrollTop = conversation.current.scrollHeight}, [selected?.history.at(-1)?.id, selected?.task_id, dialogue.at(-1)?.id, dialogue.at(-1)?.text, dialogue.at(-1)?.output, following])
  useEffect(() => {const el = composer.current; if (el) {el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 180)}px`}}, [prompt, view])
  const writeMessage = (text?: string) => {
    setMobileNav(false); setView('chat')
    if (text !== undefined) setPrompt(text)
    requestAnimationFrame(() => composer.current?.focus())
  }
  const mutate = async (path: string, body: unknown) => {
    try {
      const result = await api<{queued?: boolean; reason?: string; next_check_at?: string}>(path, body)
      if (result.queued === false) setToast(result.reason === 'already_checking' ? '已经在确认页面了' : '为避免过于频繁的请求，会在允许的时间确认')
      refresh()
      return result
    } catch (e) {setToast(e instanceof Error ? e.message : '操作暂时没有完成'); throw e}
  }
  // The proposal's own digest is the guard: the server refuses to start a watch whose plan
  // has changed since the person saw it, so a stale card cannot silently activate something else.
  const startWatch = async (draft: Draft) => {
    await api(`/pulse/watches/${draft.task_id}/start`, {spec_digest: draft.spec_digest})
    setSelectedId(draft.task_id)
    refresh()
  }
  const onCompose = async (e: FormEvent) => {    e.preventDefault()
    if ((!prompt.trim() && !images.length) || messageBusy || (agentWorking && !pi)) return
    const text = prompt.trim()
    setPrompt(''); setMessageBusy(true); setFollowing(true)
    try {await api('/pulse/chat', {message: text, current_task: selected?.task_id, ...(images.length ? {images} : {}), ...(pi && agentWorking ? {streamingBehavior:queueMode} : {})}); setImages([]); refresh()}
    catch (error) {setToast(error instanceof Error ? error.message : '这次没有完成理解'); setPrompt(text)}
    finally {setMessageBusy(false)}
  }
  const sessionAction = async (command:string, params:Record<string, unknown> = {}) => {
    setMessageBusy(true)
    try {const result = await piCommand<{cancelled?:boolean}>(command, params); if (result.cancelled) setToast('扩展取消了会话切换。'); else {setPrompt(''); setImages([]); setView('chat'); setMobileNav(false)}; refresh()}
    catch (e) {setToast(e instanceof Error ? e.message : '会话操作没有完成')} finally {setMessageBusy(false)}
  }
  const stop = async () => {
    try {const queued = await piCommand<{steering:string[]; followUp:string[]}>('clear_queue'); await piCommand('abort');
      const restored = [...queued.steering, ...queued.followUp].join('\n\n'); if (restored) setPrompt(current => [current, restored].filter(Boolean).join('\n\n')); refresh()}
    catch (e) {setToast(e instanceof Error ? e.message : '停止没有完成')}
  }
  return <div className="app-shell">
    {mobileNav && <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMobileNav(false)}/>}
    <aside ref={navigation} className={`sidebar ${mobileNav ? 'mobile-open' : ''}`} inert={smallScreen && !mobileNav} aria-label="任务导航">
      <a className="brand" href="/" aria-label="Morning 首页"><span className="brand-mark"><MorningMark size={48} reveal/></span><span>morning<span className="brand-period">.</span><small>你的个人工作空间</small></span></a>
      <button className="compose-nav" disabled={!!pi && agentWorking} onClick={() => pi ? void sessionAction('new_session') : writeMessage()}><SquarePen size={16}/>{pi ? '新建会话' : '写消息'}<ChevronRight size={14}/></button>
      <div className="nav-section-label">工作空间</div>
      <button className={`nav-item ${view === 'chat' ? 'selected' : ''}`} aria-current={view === 'chat' ? 'page' : undefined}
        onClick={() => {setView('chat'); setMobileNav(false)}}><MessageSquare size={17}/><span>Agent 对话</span></button>
      {data?.platform_available && (['computer','watch','tasks','devices'] as const).map((page,index) => {const Icon=[Monitor,Eye,ListTodo,Laptop][index];return <button key={page} className={`nav-item ${view===page?'selected':''}`} aria-current={view===page?'page':undefined} onClick={()=>{setView(page);setMobileNav(false)}}><Icon size={17}/><span>{['Computer','Watch','Tasks','Devices'][index]}</span></button>})}
      {!!pi && sessions.length>0 && <><div className="nav-section-label">最近会话</div>
        <div className="session-nav">{sessions.slice(0, 5).map(session => <button key={session.id} className={session.id === pi.state?.sessionId ? 'selected' : ''} disabled={agentWorking} onClick={() => void sessionAction('switch_session', {sessionPath:session.path})} title={session.name || session.firstMessage}>{session.name || session.firstMessage || '未命名会话'}</button>)}</div></>}
      <div className="nav-section-label watch-section-label"><span>后台驻守</span><span>{tasks.length}</span></div>
      <div className="watch-nav">{tasks.map(t => <button key={t.task_id} onClick={() => {setSelectedId(t.task_id); setFollowing(true); setMobileNav(false); setView('chat')}} className={selected?.task_id === t.task_id ? 'selected' : ''}>
        <span className={`nav-dot ${t.state}`}/><span>{t.label}</span>{t.state === 'approval' && <AlertCircle size={13}/>}</button>)}</div>
      {!tasks.length && <div className="nav-empty"><Eye size={17}/><p>暂时没有驻守任务。<span>需要时，在对话中告诉 Morning。</span></p></div>}
      <div className="sidebar-bottom">
        <div className="sidebar-art" aria-hidden="true"><MorningMark size={148}/><span>每一天，都有新的可能。</span></div>
        {workspaceName && <div className="workspace-label" title={data?.workspace || ''}><FolderOpen size={16}/><span><small>当前工作区</small>{workspaceName}</span></div>}
        <button className={`nav-item settings-nav ${view === 'settings' ? 'selected' : ''}`} aria-current={view === 'settings' ? 'page' : undefined}
          onClick={() => {setView('settings'); setMobileNav(false)}}><SettingsIcon size={17}/><span>设置</span></button>
        <div className="sidebar-foot"><span className={`connection-dot ${error ? 'offline' : ''}`}/><span>{error ? '服务连接中断' : data ? '本地服务已连接' : '正在连接…'}</span></div>
      </div>
    </aside>
    <main className="main-shell" inert={smallScreen && mobileNav}><header className="app-header">
      <div className="header-heading"><button className="icon-button mobile-menu" aria-label="打开导航" aria-expanded={mobileNav} onClick={() => setMobileNav(true)}><Menu size={20}/></button>
        <div><strong>{view !== 'chat' ? ({settings:'设置',computer:'Computer',watch:'Watch',tasks:'Tasks',devices:'Devices'} as const)[view] : <><span className="desktop-title">{pi?.state?.sessionName || '对话'}</span><span className="mobile-wordmark">morning<span>.</span></span></>}</strong><span>{view !== 'chat' ? ({settings:'让 Morning 适合你的习惯',computer:'观察 · 操作 · 核验',watch:'持续关注现实中的变化',tasks:'执行记录与后台通知',devices:'连接和授权你的工作环境'} as const)[view] : pi?.state?.model ? `${pi.state.model.name} · ${pi.state.thinkingLevel}` : 'Morning · 你的工作助手'}</span></div></div>
      <StatusBar watch={pulseWatch} now={now+clockOffset} connectionError={error} notice={notice} onNoticeClose={() => setNotice(null)} mutate={mutate}
        computerNotice={computerUrgent?{state:computerUrgent.state==='approval'?'approval':'attention',text:computerUrgent.goal,open:()=>setView('computer')}:undefined} orbHue={app.settings?.orb_hue ?? null} agentWorking={agentWorking} onReconnect={refresh}
        onRestart={() => writeMessage(pulseWatch?.configuration.spec.purpose || '')}
        onSettings={() => {if (pulseWatch) setSelectedId(pulseWatch.task_id); setSettings(true)}}/>
    </header>
      {error && <div className="connection-banner" role="status"><AlertCircle size={15}/><span>暂时无法连接本地服务，正在重试。</span><button onClick={refresh}><RotateCw size={13}/>重新连接</button></div>}
      {view === 'settings' ? <div className="conversation settings-scroll"><SettingsView settings={app.settings} save={app.save} error={app.error}
        capabilities={<>{pi&&<section className="settings-group"><div className="section-heading"><div><h2>Pi 原生能力</h2><p className="settings-hint">模型提供商认证、上下文、Skills、Extensions、MCP 与工具。</p></div><button className="secondary-button" onClick={()=>setNativePanel(true)}>打开 Agent 工作空间<ChevronRight size={14}/></button></div></section>}{platform.data&&<PlatformModules data={platform.data} refresh={platform.refresh}/>}</>} state={data} tasks={tasks} onReady={() => {setView('chat'); refresh()}} onOpenWatch={id => {setSelectedId(id); setSettings(true)}}/></div> : view !== 'chat' ? <PlatformView page={view} data={platform.data} error={platform.error} refresh={platform.refresh} busyAgent={agentWorking} onSession={path=>void sessionAction('switch_session',{sessionPath:path})} onSettings={watch=>{setSelectedId(watch.task_id);setSettings(true)}} onAsk={text=>{setView('chat');setMessageBusy(true);void api('/pulse/chat',{message:text,...(agentWorking?{streamingBehavior:'followUp'}:{})}).then(()=>{refresh();platform.refresh()}).catch(e=>{setToast(e.message);setPrompt(text)}).finally(()=>setMessageBusy(false))}}/> : <>
      <div className="conversation" ref={conversation} onScroll={e => {const el=e.currentTarget; setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight < 70)}}>
        {dialogue.length === 0 ? <div className="empty-state"><div className="welcome-art"><MorningMark size={290} reveal interactive label="重播分层日出 Logo"/></div>
          <div className="eyebrow">A NEW DAY WITH MORNING</div><h1>从一句话开始。</h1><p>把想法交给 Morning，<br/>一起把下一步做好。</p>
        </div> : <Timeline messages={dialogue} onStartWatch={startWatch} startedTasks={new Set(tasks.map(t => t.task_id))}
          onRetry={text => writeMessage(text)} onLogin={(url, finish) => mutate('/pulse/login', {url, finish})}/>}
        {platform.data && <ComputerApprovals jobs={platform.data.computer.filter(job=>job.session_path===pi?.state?.sessionFile)} refresh={platform.refresh}/>}
        {agentWorking && <div className="agent-thinking" role="status"><span className="thinking-dot"/>Agent 正在处理你的任务…</div>}
      </div>
      <footer className="composer-wrap">
        {pi && !pi.agent_ready && <div className="pi-start-hint"><span>连接模型网关后，即可使用 Pi 的完整工作会话。</span><button className="text-button" onClick={() => setView('settings')}>连接设置<ChevronRight size={13}/></button></div>}
        {pi && Object.entries(pi.widgets || {}).map(([key, lines]) => lines.length > 0 && <div className="pi-widget" key={key}>{lines.map((line, i) => <div key={i}>{line}</div>)}</div>)}
        {pi && Object.values(pi.statuses || {}).some(Boolean) && <div className="pi-extension-status" role="status">{Object.values(pi.statuses).filter(Boolean).join(' · ')}</div>}
        {!following && <button className="jump-latest" onClick={() => {setFollowing(true); conversation.current?.scrollTo({top: conversation.current.scrollHeight, behavior: 'smooth'})}}><ArrowDown size={14}/>回到最新</button>}
        <form className={`composer${agentWorking ? ' is-working' : ''}`} onSubmit={e => void onCompose(e)}>
          {!!images.length && <div className="composer-images">{images.map((image, index) => <span key={index}><img src={`data:${image.mimeType};base64,${image.data}`} alt={`附件 ${index + 1}`}/><button type="button" className="icon-button" aria-label={`移除附件 ${index + 1}`} onClick={() => setImages(images.filter((_, i) => i !== index))}><X size={13}/></button></span>)}</div>}
          <textarea ref={composer} rows={1} aria-label="给 Agent 的消息" value={prompt} onChange={e => setPrompt(e.target.value)}
            onKeyDown={e => {if(e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); void onCompose(e)}}} placeholder="告诉 Morning，你想完成什么…"/>
          <div className="composer-actions"><span className="composer-identity"><MorningMark size={28} active={agentWorking}/><strong>Morning</strong><span>{agentWorking ? '正在处理' : 'Agent'}</span></span>
            {pi && <div className="composer-native-actions"><input ref={attachment} type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden onChange={e => {
              const files = Array.from(e.target.files || []); e.target.value = ''
              if (files.reduce((sum, file) => sum + file.size, 0) > 10 * 1024 * 1024) {setToast('单条消息的图片总大小请保持在 10 MB 以内。'); return}
              void Promise.all(files.map(file => new Promise<PiImage>((resolve, reject) => {const reader = new FileReader(); reader.onerror = reject; reader.onload = () => resolve({type:'image', mimeType:file.type, data:String(reader.result).split(',')[1]}); reader.readAsDataURL(file)}))).then(added => setImages(current => [...current, ...added])).catch(() => setToast('图片没有读入，请重试。'))
            }}/><button type="button" className="icon-button" aria-label="添加图片" onClick={() => attachment.current?.click()}><Paperclip size={16}/></button>
              <button type="button" className="icon-button" aria-label="模型与 Agent 设置" onClick={() => setNativePanel(true)}><SlidersHorizontal size={16}/></button>
              {agentWorking && <><select aria-label="处理中发送方式" value={queueMode} onChange={e => setQueueMode(e.target.value as 'steer' | 'followUp')}><option value="steer">补充指令</option><option value="followUp">完成后再做</option></select><button type="button" className="stop-button" aria-label="停止 Agent" onClick={() => void stop()}><Square size={13} fill="currentColor"/></button></>}
            </div>}
            <button className="send-button" disabled={(!prompt.trim() && !images.length) || messageBusy || (agentWorking && !pi)} aria-label="发送消息"><ArrowUp size={18}/></button></div>
        </form>
        <div className="composer-foot"><span>{pi ? <><Check size={12}/>{pi.state?.pendingMessageCount ? `${pi.state.pendingMessageCount} 条消息等待处理` : pi.agent_ready ? 'Pi 原生会话 · 自动保存' : 'Pi 已就绪'}</> : data?.understanding_available ? <><Check size={12}/>由你决定下一步</> : '任务理解模型尚未连接'}</span><span>Enter 发送 <i>·</i> Shift + Enter 换行</span></div>
      </footer>
      </>}
    </main>
    {toast && <div className="toast" role="status"><AlertCircle size={15}/>{toast}<button className="icon-button" aria-label="关闭提示" onClick={() => setToast('')}><X size={14}/></button></div>}
    {settings && selected && <WatchSettings watch={selected} close={() => setSettings(false)} mutate={mutate}/>}
    {nativePanel && pi && <NativePanel pi={pi} close={() => setNativePanel(false)} refresh={refresh} onInsert={text => writeMessage(text)} onSession={() => {setView('chat'); setPrompt(''); setImages([])}}/>}
    {pi?.pending_ui?.[0] && <NativeInteraction key={pi.pending_ui[0].id} request={pi.pending_ui[0]} refresh={refresh}/>}
  </div>
}
