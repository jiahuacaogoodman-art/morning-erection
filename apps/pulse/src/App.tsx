import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, ArrowDown, ArrowUp, Check, ChevronRight, Menu, PanelLeftClose, PanelLeftOpen, Paperclip, RotateCw, SlidersHorizontal, Square, X } from 'lucide-react'
import { api, ApiError } from './api'
import { Dialog } from './Dialog'
import { StatusBar } from './StatusBar'
import { Timeline } from './Timeline'
import { SettingsView, alertUser, useSettings } from './Settings'
import type { Change, Draft, PiImage, State, Watch } from './types'
import { MorningMark } from './Brand'
import { NativeInteraction, NativePanel, piCommand, piControl } from './NativePi'
import type {PiSession} from './NativePi'
import {ComputerApprovals, PlatformModules, PlatformView, usePlatform} from './Platform'
import {Sidebar} from './Sidebar'
import {ModelPicker} from './ModelPicker'
import {useThinkingLevel} from './ThinkingControl'
import {useComposerCommands} from './ComposerCommands'
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
  const [sidebarExpanded,setSidebarExpanded] = useState(()=>localStorage.getItem('morning:sidebar')==='expanded')
  useEffect(()=>{localStorage.setItem('morning:sidebar',sidebarExpanded?'expanded':'collapsed')},[sidebarExpanded])
  useEffect(()=>{const key=(event:KeyboardEvent)=>{if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==='b'&&!(event.target instanceof Element&&event.target.closest('input,textarea,[contenteditable=true]'))){event.preventDefault();setSidebarExpanded(value=>!value)}};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key)},[])
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
  const thinking = useThinkingLevel(pi,agentWorking,refresh)
  const commands = useComposerCommands({pi,thinking,text:prompt,setText:setPrompt,input:composer})
  const [following, setFollowing] = useState(true)
  const tasks = ((!platform.error && platform.data?.watches) || data?.items || []).filter(t => !['DRAFT','CANCELLED','EXPIRED'].includes(t.lifecycle))
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
      refresh(); platform.refresh()
      return result
    } catch (e) {setToast(e instanceof Error ? e.message : '操作暂时没有完成'); throw e}
  }
  // The proposal's own digest is the guard: the server refuses to start a watch whose plan
  // has changed since the person saw it, so a stale card cannot silently activate something else.
  const startWatch = async (draft: Draft) => {
    await api(`/pulse/watches/${draft.task_id}/start`, {spec_digest: draft.spec_digest})
    setSelectedId(draft.task_id)
    refresh(); platform.refresh()
  }
  const onCompose = async (e: FormEvent) => {    e.preventDefault()
    if(thinking.changing||commands.interceptSubmit())return
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
    <button className={`nav-scrim ${mobileNav?'is-open':''}`} aria-label="关闭导航" aria-hidden={!mobileNav} inert={!mobileNav} onClick={()=>setMobileNav(false)}/>
    <Sidebar navigationRef={navigation} expanded={sidebarExpanded} mobile={smallScreen} mobileOpen={mobileNav} view={view} platformAvailable={!!data?.platform_available} hasPi={!!pi} sessions={sessions} sessionId={pi?.state?.sessionId} tasks={tasks} selectedId={selected?.task_id} workspace={data?.workspace} busy={agentWorking} connected={!!data} error={error}
      onNew={()=>pi?void sessionAction('new_session'):writeMessage()} onView={next=>{setView(next as typeof view);setMobileNav(false)}} onSession={session=>void sessionAction('switch_session',{sessionPath:session.path})} onWatch={task=>{setSelectedId(task.task_id);setFollowing(true);setMobileNav(false);setView('chat')}} onHistory={()=>{setNativePanel(true);setMobileNav(false)}}/>
    <main className="main-shell" inert={smallScreen && mobileNav}><header className="app-header">
      <div className="header-heading"><button className="icon-button sidebar-toggle" aria-label={sidebarExpanded?'收起侧栏':'展开侧栏'} title="切换侧栏 · ⌘B / Ctrl+B" aria-expanded={sidebarExpanded} onClick={()=>setSidebarExpanded(value=>!value)}>{sidebarExpanded?<PanelLeftClose size={18}/>:<PanelLeftOpen size={18}/>}</button><button className="icon-button mobile-menu" aria-label="打开导航" aria-expanded={mobileNav} onClick={() => setMobileNav(true)}><Menu size={20}/></button>
        <div><strong>{view !== 'chat' ? ({settings:'设置',computer:'Computer',watch:'Watch',tasks:'Tasks',devices:'Devices'} as const)[view] : <><span className="desktop-title">{pi?.state?.sessionName || (dialogue.length?'工作会话':'新会话')}</span><span className="mobile-wordmark">morning<span>.</span></span></>}</strong><span>{view !== 'chat' ? ({settings:'让 Morning 适合你的习惯',computer:'观察 · 操作 · 核验',watch:'持续关注现实中的变化',tasks:'执行记录与后台通知',devices:'连接和授权你的工作环境'} as const)[view] : agentWorking ? '正在处理你的任务' : '会话自动保存'}</span></div></div>
      <StatusBar watch={pulseWatch} watches={tasks} onSelectWatch={id=>setSelectedId(id)} now={now+clockOffset} connectionError={error} notice={notice} onNoticeClose={() => setNotice(null)} mutate={mutate}
        computerNotice={computerUrgent?{state:computerUrgent.state==='approval'?'approval':'attention',text:computerUrgent.goal,open:()=>setView('computer')}:undefined} orbHue={app.settings?.orb_hue ?? null} agentWorking={agentWorking} onReconnect={refresh}
        onRestart={() => writeMessage(pulseWatch?.configuration.spec.purpose || '')}
        onSettings={() => {if (pulseWatch) setSelectedId(pulseWatch.task_id); setSettings(true)}}/>
    </header>
      {error && <div className="connection-banner" role="status"><AlertCircle size={15}/><span>暂时无法连接本地服务，正在重试。</span><button onClick={refresh}><RotateCw size={13}/>重新连接</button></div>}
      {view === 'settings' ? <div className="conversation settings-scroll"><SettingsView settings={app.settings} save={app.save} error={app.error}
        capabilities={platform.data&&<PlatformModules data={platform.data} refresh={platform.refresh}/>} refreshAgent={refresh} onInsert={text=>writeMessage(text)} state={data} tasks={tasks} onReady={() => {setView('chat'); refresh()}} onOpenWatch={id => {setSelectedId(id); setSettings(true)}}/></div> : view !== 'chat' ? <PlatformView page={view} data={platform.data} error={platform.error} refresh={platform.refresh} busyAgent={agentWorking} onSession={path=>void sessionAction('switch_session',{sessionPath:path})} onSettings={watch=>{setSelectedId(watch.task_id);setSettings(true)}} onAsk={text=>{setView('chat');setMessageBusy(true);void api('/pulse/chat',{message:text,...(agentWorking?{streamingBehavior:'followUp'}:{})}).then(()=>{refresh();platform.refresh()}).catch(e=>{setToast(e.message);setPrompt(text)}).finally(()=>setMessageBusy(false))}}/> : <>
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
          {commands.menu}
          {!!images.length && <div className="composer-images">{images.map((image, index) => <span key={index}><img src={`data:${image.mimeType};base64,${image.data}`} alt={`附件 ${index + 1}`}/><button type="button" className="icon-button" aria-label={`移除附件 ${index + 1}`} onClick={() => setImages(images.filter((_, i) => i !== index))}><X size={13}/></button></span>)}</div>}
          <textarea ref={composer} rows={1} aria-label="给 Agent 的消息" value={prompt} onChange={e => {setPrompt(e.target.value);commands.trackCaret()}} onSelect={commands.trackCaret} {...(pi?commands.inputProps:{})}
            onKeyDown={e => {if(commands.keyDown(e))return;if(e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {e.preventDefault(); void onCompose(e)}}} placeholder="告诉 Morning，你想做什么… /"/>
          <div className="composer-actions">{pi?<ModelPicker pi={pi} busy={agentWorking||thinking.changing} refresh={refresh} thinking={thinking}/>:<span className="composer-identity"><MorningMark size={28} active={agentWorking}/><strong>Morning</strong><span>{agentWorking?'正在处理':'Agent'}</span></span>}
            {pi && <div className="composer-native-actions"><input ref={attachment} type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden onChange={e => {
              const files = Array.from(e.target.files || []); e.target.value = ''
              if (files.reduce((sum, file) => sum + file.size, 0) > 10 * 1024 * 1024) {setToast('单条消息的图片总大小请保持在 10 MB 以内。'); return}
              void Promise.all(files.map(file => new Promise<PiImage>((resolve, reject) => {const reader = new FileReader(); reader.onerror = reject; reader.onload = () => resolve({type:'image', mimeType:file.type, data:String(reader.result).split(',')[1]}); reader.readAsDataURL(file)}))).then(added => setImages(current => [...current, ...added])).catch(() => setToast('图片没有读入，请重试。'))
            }}/><button type="button" className="icon-button" aria-label="添加图片" onClick={() => attachment.current?.click()}><Paperclip size={16}/></button>
              <button type="button" className="icon-button" aria-label="模型与 Agent 设置" onClick={() => setNativePanel(true)}><SlidersHorizontal size={16}/></button>
              {agentWorking && <><select aria-label="处理中发送方式" value={queueMode} onChange={e => setQueueMode(e.target.value as 'steer' | 'followUp')}><option value="steer">补充指令</option><option value="followUp">完成后再做</option></select><button type="button" className="stop-button" aria-label="停止 Agent" onClick={() => void stop()}><Square size={13} fill="currentColor"/></button></>}
            </div>}
            <button className="send-button" disabled={(!prompt.trim() && !images.length) || messageBusy || thinking.changing || (agentWorking && !pi)} aria-label="发送消息"><ArrowUp size={18}/></button></div>
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
