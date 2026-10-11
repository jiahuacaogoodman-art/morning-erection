/* Settings — app-wide preferences, plus a read-only view of what this Pulse is connected to.
   Appearance, notifications and default cadence apply immediately and persist server-side
   (GET/POST /pulse/settings). The model is chosen when Pulse starts, so it is shown, not edited.
*/
import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowUpRight, Bell, BellOff, Blocks, Check, GitBranch, Info, KeyRound, Monitor, Moon, Palette, Search, SlidersHorizontal, Sun, X } from 'lucide-react'
import { api } from './api'
import { Cadence } from './Cadence'
import { GatewaySettings } from './Gateways'
import { NativePanel,piControl } from './NativePi'
import { NativePreferences, useNativePreferences } from './NativePreferences'
import { Disclosure, SlidingTabs } from './Motion'
import { Orb } from './Orb'
import {FieldHelp} from './FieldHelp'
import type { OrbState } from './Orb'
import type { AppSettings, SettingsResponse, State, Theme, Watch } from './types'

const CACHE = 'pulse:settings'
// Only the user's own preferences are cached locally; the `_`-prefixed fields are derived by
// the server on every read and have no business sitting in browser storage.
const KEYS: (keyof AppSettings)[] = ['theme', 'locale', 'notify_on_change', 'notify_on_approval', 'notify_sound',
  'polling_min_seconds', 'polling_max_seconds', 'orb_hue']

function cache(value: AppSettings) {
  const picked: Partial<AppSettings> = {}
  for (const key of KEYS) (picked as Record<string, unknown>)[key] = value[key]
  try { localStorage.setItem(CACHE, JSON.stringify(picked)) } catch { /* private mode */ }
}

function cached(): SettingsResponse | null {
  try { return JSON.parse(localStorage.getItem(CACHE) || 'null') } catch { return null }
}

/* Resolve `system` against the OS and write the result onto <html data-theme>; the CSS only
   ever sees light or dark. */
export function applyTheme(theme: Theme | undefined) {
  const dark = theme === 'dark' || (theme !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  // Change the palette in one paint; independently fading foreground/background
  // colors briefly produces unreadable text when switching light and dark.
  document.documentElement.classList.add('theme-change')
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  requestAnimationFrame(()=>requestAnimationFrame(()=>document.documentElement.classList.remove('theme-change')))
}

export function applyCachedTheme() { applyTheme(cached()?.theme) }

export function useSettings() {
  const [settings, setSettings] = useState<SettingsResponse | null>(cached)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    api<SettingsResponse>('/pulse/settings').then(value => {
      if (!live) return
      setSettings(value); cache(value)
    }).catch(() => { /* the cached copy (or defaults) keeps the UI usable while offline */ })
    return () => { live = false }
  }, [])
  const theme = settings?.theme || 'system'
  useEffect(() => {
    applyTheme(theme)
    if (theme !== 'system') return
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const change = () => applyTheme('system')
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [theme])
  // Optimistic: the control moves at once; a rejected save puts it back and says why.
  const save = async (patch: Partial<AppSettings>) => {
    const previous = settings
    setError('')
    if (previous) setSettings({...previous, ...patch})
    try {
      const value = await api<AppSettings>('/pulse/settings', patch)
      setSettings(current => {
        const next = {...(current || previous || {_model_configured: false, _data_dir: null}), ...value} as SettingsResponse
        cache(next)
        return next
      })
    } catch (e) {
      setSettings(previous)
      setError(e instanceof Error ? e.message : '设置没有保存')
      throw e
    }
  }
  return {settings, save, error}
}

/* ── alerts: sound + system notification ── */
function chime() {
  try {
    const ctx = new AudioContext()
    const gain = ctx.createGain()
    gain.connect(ctx.destination)
    gain.gain.setValueAtTime(0.0001, ctx.currentTime)
    gain.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.5)
    for (const [freq, at] of [[880, 0], [1320, 0.12]]) {
      const osc = ctx.createOscillator()
      osc.type = 'sine'
      osc.frequency.value = freq
      osc.connect(gain)
      osc.start(ctx.currentTime + at)
      osc.stop(ctx.currentTime + 0.5)
    }
    setTimeout(() => void ctx.close(), 800)
  } catch { /* audio is a nicety; some browsers block it until the page is interacted with */ }
}

export function alertUser(settings: AppSettings | null, kind: 'change' | 'approval', title: string, body: string) {
  if (!settings) return
  if (kind === 'change' ? !settings.notify_on_change : !settings.notify_on_approval) return
  if (settings.notify_sound) chime()
  // The in-page status bar already shows it when the tab is visible; the OS banner is for when it is not.
  if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
    new Notification(title, {body, tag: `pulse-${kind}`})
  }
}

/* ── small controls ── */
function Group({title, hint, children}: {title: string; hint?: string; children: ReactNode}) {
  const id = useId()
  return <section className="settings-group" aria-labelledby={id}>
    <h2 id={id}>{title}</h2>
    {hint && <p className="settings-hint">{hint}</p>}
    <div className="settings-rows">{children}</div>
  </section>
}

function Row({label, hint, children, labelId}: {label: string; hint?: ReactNode; children?: ReactNode; labelId?: string}) {
  return <div className="settings-row">
    <div className="settings-row-text"><span id={labelId} className="settings-row-label">{label}</span>
      {hint && <FieldHelp label={label}>{hint}</FieldHelp>}</div>
    {children && <div className="settings-row-control">{children}</div>}
  </div>
}

function Toggle({label, hint, checked, disabled, onChange}: {label: string; hint?: string; checked: boolean; disabled?: boolean; onChange: (v: boolean) => void}) {
  const id = useId()
  return <Row label={label} hint={hint} labelId={id}>
    <button type="button" role="switch" aria-checked={checked} aria-labelledby={id} disabled={disabled}
      className="switch" onClick={() => onChange(!checked)}><span className="switch-knob"/></button>
  </Row>
}

function Fact({label, children}: {label: string; children: ReactNode}) {
  return <div className="settings-fact"><dt>{label}</dt><dd>{children}</dd></div>
}

function Status({ok, yes, no}: {ok: boolean; yes: string; no: string}) {
  return <span className={`settings-status ${ok ? 'ok' : ''}`}><span className="settings-status-dot"/>{ok ? yes : no}</span>
}

/* The five orb states, in the order a watch moves through them: quiet, busy, waiting on you,
   something wrong, disconnected. Their order is the point — the strip shows which moods the
   hue reaches and which one it deliberately leaves alone. */
const ORB_STATES: {state: OrbState; label: string}[] = [
  {state: 'idle', label: '驻守'}, {state: 'working', label: '处理'}, {state: 'approval', label: '待批准'},
  {state: 'error', label: '需确认'}, {state: 'offline', label: '离线'}]

function Hue({hue, save}: {hue: number | null; save: (patch: Partial<AppSettings>) => Promise<void>}) {
  const labelId = useId()
  // The slider never sits at "no value": it shows the hue actually on screen, which is the
  // state's own while following. Dragging is what opts into a custom hue.
  const [value, setValue] = useState(hue ?? 0)
  const [following, setFollowing] = useState(hue == null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {if (hue == null) {setValue(0); setFollowing(true)} else {setValue(hue); setFollowing(false)}}, [hue])
  useEffect(() => () => {if (timer.current) clearTimeout(timer.current)}, [])

  const commit = (next: number | null) => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => { void save({orb_hue: next}).catch(() => {}) }, 180)
  }
  const custom = following ? null : value

  return <>
    <Row label="状态圆点" labelId={labelId} hint={following ? '跟随状态自身的颜色' : '五种状态共用这个色相，离线的灰色保持不变'}>
      <div className="settings-hue">
        <input type="range" min="0" max="359" step="1" value={value} aria-labelledby={labelId}
          aria-valuetext={following ? '跟随状态' : `色相 ${value} 度`}
          onChange={e => {const next = Number(e.target.value); setFollowing(false); setValue(next); commit(next)}}
          style={{'--hue': `${value}`} as React.CSSProperties}/>
        <button type="button" className={`secondary-button${following ? ' selected' : ''}`} aria-pressed={following}
          onClick={() => {setFollowing(true); commit(null)}}>{following && <Check size={12}/>}跟随状态</button>
      </div>
    </Row>
    <Row label="外观预览" hint="拖动上面的色相，这里和状态条会一起变化">
      <div className="settings-swatch-strip">
        {ORB_STATES.map(({state, label}) => <span key={state} className="settings-swatch">
          <Orb state={state} size={13} hue={custom}/><small>{label}</small></span>)}
      </div>
    </Row>
  </>
}

const THEMES: {value: Theme; label: string; Icon: typeof Sun}[] = [
  {value: 'light', label: '浅色', Icon: Sun}, {value: 'dark', label: '深色', Icon: Moon}, {value: 'system', label: '跟随系统', Icon: Monitor}]

const stateNames: Record<string, string> = {watching: '驻守中', working: '正在处理', approval: '等待批准', deferred: '等待时段',
  attention: '需要确认', reconnecting: '正在重连', connecting: '正在连接', paused: '已暂停', completed: '已完成'}

/* ── the page ── */
interface SettingsViewProps {
  settings: SettingsResponse | null
  save: (patch: Partial<AppSettings>) => Promise<void>
  error: string
  state: State | null
  tasks: Watch[]
  onOpenWatch: (taskId: string) => void
  onReady?: () => void
  capabilities?: ReactNode
  refreshAgent?: () => void
  onInsert?: (text:string) => void
}

export function SettingsView({settings, save, error, state, tasks, onOpenWatch, onReady, capabilities, refreshAgent, onInsert}: SettingsViewProps) {
  const categories = [
    {id:'connections',label:'模型与连接',Icon:KeyRound,description:'模型服务、认证与当前会话',keywords:'Gateway 凭据 账户 OAuth 模型'},
    {id:'agent',label:'Agent 行为',Icon:SlidersHorizontal,description:'消息队列、重试、连接和图片',keywords:'重试 网络 缓存 图片 思考 信任'},
    {id:'context',label:'会话与上下文',Icon:GitBranch,description:'历史、分支与上下文策略',keywords:'历史 分支 导出 压缩 预算'},
    {id:'resources',label:'能力与扩展',Icon:Blocks,description:'原生工具、Skills 与 Extensions',keywords:'工具 Skills Extensions MCP 包 路径'},
    {id:'appearance',label:'外观',Icon:Palette,description:'主题与状态点的视觉偏好',keywords:'主题 深色 浅色 颜色 状态点'},
    {id:'background',label:'通知与后台',Icon:Bell,description:'提醒、后台能力与驻守节奏',keywords:'通知 提示音 Watch Computer 频率 暂停'},
    {id:'about',label:'运行环境',Icon:Info,description:'服务状态和本地数据位置',keywords:'状态 数据 工作区 诊断'},
  ]
  const routeCategory = () => {const value=location.hash.split('/')[1];return categories.some(item=>item.id===value)?value:'connections'}
  const [category,setCategory] = useState(routeCategory)
  const [runtimeError,setRuntimeError]=useState(''),[runtimeBusy,setRuntimeBusy]=useState(false)
  const [query,setQuery] = useState(''), [focusKey,setFocusKey] = useState('')
  const [connectionTab,setConnectionTab] = useState('gateway'), [contextTab,setContextTab] = useState('history'), [resourceTab,setResourceTab] = useState('loaded')
  const native = useNativePreferences(!!state?.pi)
  const themeLabel = useId()
  const supported = 'Notification' in window
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(supported ? Notification.permission : 'unsupported')
  const [minimum, setMinimum] = useState(String(settings?.polling_min_seconds ?? 10)), [maximum, setMaximum] = useState(String(settings?.polling_max_seconds ?? 120))
  const [cadenceError, setCadenceError] = useState(''), [cadenceSaved, setCadenceSaved] = useState(false)
  const choose = (next:string) => {setCategory(next);location.hash='settings/'+next;setQuery('')}
  useEffect(()=>{const changed=()=>setCategory(routeCategory());window.addEventListener('hashchange',changed);return()=>window.removeEventListener('hashchange',changed)},[])
  useEffect(() => {if(settings){setMinimum(String(settings.polling_min_seconds));setMaximum(String(settings.polling_max_seconds))}},[settings?.polling_min_seconds,settings?.polling_max_seconds])
  useEffect(() => {if(!cadenceSaved)return;const t=setTimeout(()=>setCadenceSaved(false),2200);return()=>clearTimeout(t)},[cadenceSaved])
  useEffect(()=>{if(!focusKey)return;const timer=setTimeout(()=>{const row=document.getElementById('setting-'+focusKey);row?.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});row?.querySelector<HTMLElement>('input,select,button,textarea')?.focus({preventScroll:true})},280);return()=>clearTimeout(timer)},[focusKey,category])
  const groupCategory=(group:string)=>['context','budgets'].includes(group)?'context':['tools','resources'].includes(group)?'resources':'agent'
  const searchResults=query.trim()?[...categories.filter(item=>`${item.label} ${item.keywords}`.toLowerCase().includes(query.toLowerCase())).map(item=>({key:item.id,label:item.label,category:item.id,group:''})),...(native.data?.fields||[]).filter(item=>`${item.label} ${item.key} ${item.help}`.toLowerCase().includes(query.toLowerCase())).map(item=>({key:item.key,label:item.label,category:groupCategory(item.group),group:item.group}))].slice(0,12):[]
  const searchSelect=(item:typeof searchResults[number])=>{choose(item.category);if(item.group){setFocusKey(item.key);if(item.category==='context')setContextTab('policy');if(item.category==='resources')setResourceTab(item.group==='resources'?'paths':'policy')}}
  const active=categories.find(item=>item.id===category)!
  if(!settings)return <div className="settings-loading" role="status">正在读取设置…</div>
  const wantsNotifications=settings.notify_on_change||settings.notify_on_approval
  const setNotify=async(patch:Partial<AppSettings>)=>{if(supported&&Object.values(patch).some(Boolean)&&Notification.permission==='default')setPermission(await Notification.requestPermission());await save(patch).catch(()=>{})}
  const dirty=minimum!==String(settings.polling_min_seconds)||maximum!==String(settings.polling_max_seconds)
  const saveCadence=async()=>{const min=Number(minimum),max=Number(maximum);if(!Number.isInteger(min)||min<2||min>3600)return setCadenceError('最短间隔应为 2 到 3600 秒的整数');if(!Number.isInteger(max)||max<min||max>86400)return setCadenceError('最长间隔应不小于最短间隔，且不超过 86400 秒');setCadenceError('');try{await save({polling_min_seconds:min,polling_max_seconds:max});setCadenceSaved(true)}catch(e){setCadenceError((e as Error).message)}}
  const runtime=state?.runtime
  const embedded=(tab:string)=>state?.pi?<NativePanel key={tab} embedded initialTab={tab} pi={state.pi} close={()=>{}} refresh={refreshAgent||(()=>{})} onSession={onReady} onInsert={onInsert||(()=>{})}/>:<p className="settings-hint">Pi 运行时尚未连接。</p>
  const preferences=(groups:string[])=>state?.pi?<NativePreferences controller={native} groups={groups} busyAgent={state.agent_busy} focusKey={focusKey}/>:<p className="settings-hint">连接 Pi 后可管理原生设置。</p>
  const subnav=(items:string[][],current:string,select:(v:string)=>void)=><SlidingTabs items={items.map(([id,label])=>({id,label}))} value={current} onChange={select} label={active.label+'分组'}/>

  return <div className="settings-workspace">
    <header className="settings-titlebar"><div><h1>设置</h1></div><div className="settings-search"><Search size={15}/><input type="search" aria-label="搜索设置" placeholder="搜索设置…" value={query} onChange={e=>setQuery(e.target.value)} onKeyDown={e=>{if(e.key==='Escape')setQuery('');if(e.key==='Enter'&&searchResults[0]){e.preventDefault();searchSelect(searchResults[0])}}}/>{query&&<button className="icon-button" aria-label="清除设置搜索" onClick={()=>setQuery('')}><X size={13}/></button>}
      {query&&<div className="settings-search-results" aria-label="设置搜索结果">{searchResults.length?searchResults.map(item=><button key={item.key} onClick={()=>searchSelect(item)}><span>{item.label}<small>{categories.find(c=>c.id===item.category)?.label}</small></span><ArrowUpRight size={13}/></button>):<p>没有找到对应设置。</p>}</div>}
    </div></header>
    <div className="settings-layout"><nav className="settings-navigation" aria-label="设置分类"><div role="tablist" aria-orientation="vertical" aria-label="设置分类">{categories.map((item,index)=><button type="button" role="tab" key={item.id} id={'settings-tab-'+item.id} aria-selected={category===item.id} aria-controls="settings-pane" tabIndex={category===item.id?0:-1} className={category===item.id?'selected':''} onClick={()=>choose(item.id)} onKeyDown={e=>{const target=e.key==='ArrowDown'?(index+1)%categories.length:e.key==='ArrowUp'?(index-1+categories.length)%categories.length:e.key==='Home'?0:e.key==='End'?categories.length-1:null;if(target!==null){e.preventDefault();choose(categories[target].id);requestAnimationFrame(()=>document.getElementById('settings-tab-'+categories[target].id)?.focus())}}}><item.Icon size={16}/><span>{item.label}</span></button>)}</div></nav>
      <label className="settings-mobile-category"><span className="sr-only">设置分类</span><select aria-label="设置分类" value={category} onChange={e=>choose(e.target.value)}>{categories.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
      <section className="settings-content" id="settings-pane" role="tabpanel" aria-labelledby={'settings-tab-'+category}>
        <div className="settings-category-head"><span className="settings-category-icon"><active.Icon size={21}/></span><div><h2>{active.label}</h2></div></div>
        {error&&<p className="inline-error" role="alert">{error}</p>}
        {native.error&&<p className="inline-error" role="alert">{native.error}{native.dirty>0&&<button className="text-button" onClick={()=>void native.discard()}>放弃修改并重新读取</button>}</p>}
        {native.notice&&<p className="settings-inline-success" role="status"><Check size={14}/>{native.notice}</p>}
        <div className="settings-category-body" key={category}>
        {category==='connections'&&<>{subnav([['gateway','模型网关'],['auth','Pi 提供商'],['model','当前模型']],connectionTab,setConnectionTab)}{connectionTab==='gateway'?(state?.pi?<GatewaySettings busyAgent={state.agent_busy} onReady={onReady}/>:<p className="settings-hint">Pi 运行时尚未连接。</p>):embedded(connectionTab)}</>}
        {category==='agent'&&preferences(['interaction','retry','transport','images','trust'])}
        {category==='context'&&<>{subnav([['history','会话历史'],['policy','上下文策略']],contextTab,setContextTab)}{contextTab==='history'?embedded('sessions'):preferences(['context','budgets'])}</>}
        {category==='resources'&&<>{subnav([['loaded','已载入能力'],['policy','工具策略'],['paths','发现路径']],resourceTab,setResourceTab)}{resourceTab==='loaded'?embedded('resources'):preferences(resourceTab==='paths'?['resources']:['tools'])}</>}
        {category==='appearance'&&<Group title="界面外观"><Row label="主题" labelId={themeLabel} hint={settings.theme==='system'?'跟随系统外观自动切换':undefined}><div className="segmented" role="radiogroup" aria-labelledby={themeLabel}>{THEMES.map(({value,label,Icon})=><button key={value} type="button" role="radio" aria-checked={settings.theme===value} className={settings.theme===value?'selected':''} onClick={()=>void save({theme:value}).catch(()=>{})}><Icon size={13} aria-hidden="true"/>{label}</button>)}</div></Row><Hue hue={settings.orb_hue} save={save}/><Row label="界面动效" hint="跟随系统的“减少动态效果”设置。开启后，收展直接到达终态。"><span className="settings-value">{matchMedia('(prefers-reduced-motion: reduce)').matches?'减少动态效果':'流畅收展'}</span></Row></Group>}
        {category==='background'&&<>
          <Group title="提醒"><Toggle label="检测到变化时" hint="驻守内容发生变化" checked={settings.notify_on_change} onChange={v=>void setNotify({notify_on_change:v})}/><Toggle label="需要你批准时" hint="Agent 准备好操作，等待确认" checked={settings.notify_on_approval} onChange={v=>void setNotify({notify_on_approval:v})}/><Toggle label="提示音" hint="提醒时播放一声轻响" checked={settings.notify_sound} onChange={v=>void save({notify_sound:v}).catch(()=>{})}/><Row label="系统通知权限" hint={permission==='granted'?'已允许':permission==='denied'?'浏览器已拒绝，请到网站权限中调整':permission==='unsupported'?'此浏览器不支持系统通知':'尚未询问'}>{permission==='default'&&<button className="secondary-button" onClick={()=>void Notification.requestPermission().then(setPermission)}><Bell size={13}/>允许通知</button>}{permission==='granted'&&<button className="secondary-button" disabled={!wantsNotifications} onClick={()=>new Notification('Morning',{body:'通知已经可以正常送达。',tag:'pulse-test'})}><Bell size={13}/>发送测试通知</button>}{permission==='denied'&&<BellOff size={15}/>}</Row></Group>
          {capabilities&&<Disclosure title="后台能力与系统提醒">{capabilities}</Disclosure>}
          <Disclosure title="新任务的检查节奏" hint="没有变化时逐步放慢；已有任务单独设置。"><form className="settings-cadence" onSubmit={e=>{e.preventDefault();void saveCadence()}}><label className="field-label">最短间隔 · 秒<input type="number" min="2" max="3600" required value={minimum} onChange={e=>setMinimum(e.target.value)}/></label><label className="field-label">最长间隔 · 秒<input type="number" min="2" max="86400" required value={maximum} onChange={e=>setMaximum(e.target.value)}/></label><button className="secondary-button" disabled={!dirty}>保存</button></form>{cadenceError&&<p className="inline-error" role="alert">{cadenceError}</p>}{cadenceSaved&&<p className="settings-saved" role="status">已保存，新任务会从这个节奏开始</p>}<Disclosure title="查看检查节奏的变化"><Cadence minSeconds={Number(minimum)||settings.polling_min_seconds} maxSeconds={Number(maximum)||settings.polling_max_seconds}/></Disclosure></Disclosure>
          <Disclosure title={<>驻守任务 <span className="setting-count">{tasks.length}</span></>}>{tasks.length===0?<Row label="还没有驻守中的任务"/>:tasks.map(t=><Row key={t.task_id} label={t.label} hint={`${stateNames[t.state]||t.lifecycle} · 约 ${t.interval_seconds} 秒一次`}><button className="secondary-button" aria-label={`${t.label}的设置`} onClick={()=>onOpenWatch(t.task_id)}>设置</button></Row>)}</Disclosure>
        </>}
        {category==='about'&&<>{state?.pi&&<Group title="并行任务"><Row label="同时载入的 Pi 会话" hint="每个会话保留独立的原生运行时；切换对话后，任务继续执行。"><select aria-label="Pi 并行会话上限" disabled={runtimeBusy} value={state.pi.runtime_limit||4} onChange={event=>{setRuntimeBusy(true);setRuntimeError('');void piControl('set_concurrency',{limit:Number(event.target.value)}).then(()=>refreshAgent?.()).catch(e=>setRuntimeError(e.message)).finally(()=>setRuntimeBusy(false))}}>{[2,3,4,5,6,7,8].map(value=><option key={value} value={value}>{value} 个会话</option>)}</select></Row>{runtimeError&&<p className="inline-error" role="alert">{runtimeError}</p>}<p className="support-copy">终端进程可独立并行；同一设备的电脑输入按顺序执行。</p></Group>}<Group title="服务状态"><dl className="settings-facts"><Fact label="任务理解"><Status ok={!!state?.understanding_available} yes="已配置" no="未配置"/></Fact><Fact label="Pi 运行时"><Status ok={!!state?.pi?.agent_ready} yes="已启动" no="尚未启动"/></Fact><Fact label="浏览器执行"><Status ok={!!runtime?.available} yes="运行中" no="未运行"/></Fact><Fact label="操作模型"><Status ok={settings._model_configured} yes="已配置" no="未配置"/></Fact></dl></Group><Group title="本地数据"><dl className="settings-facts"><Fact label="数据目录">{settings._data_dir?<code>{settings._data_dir}</code>:'仅在内存中'}</Fact>{state?.workspace&&<Fact label="工作区"><code>{state.workspace}</code></Fact>}</dl></Group></>}
        </div>
      </section>
    </div>
    {native.dirty>0&&<div className="settings-savebar" role="status"><span><i/>{native.dirty} 项 Pi 设置尚未保存<small>{native.scope==='global'?'用户设置':'当前工作区'}</small></span><div><button className="secondary-button" disabled={native.busy} onClick={()=>void native.discard()}>放弃修改</button><button className="primary-button" disabled={native.busy||state?.agent_busy} onClick={()=>void native.save()}>{native.busy?'正在保存…':'保存更改'}</button></div></div>}
  </div>
}
