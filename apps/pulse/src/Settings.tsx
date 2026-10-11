/* Settings — app-wide preferences, plus a read-only view of what this Pulse is connected to.
   Appearance, notifications and default cadence apply immediately and persist server-side
   (GET/POST /pulse/settings). The model is chosen when Pulse starts, so it is shown, not edited.
*/
import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Bell, BellOff, Check, Monitor, Moon, Sun } from 'lucide-react'
import { api } from './api'
import { Cadence } from './Cadence'
import { GatewaySettings } from './Gateways'
import { Orb } from './Orb'
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
      {hint && <span className="settings-row-hint">{hint}</span>}</div>
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
}

export function SettingsView({settings, save, error, state, tasks, onOpenWatch, onReady, capabilities}: SettingsViewProps) {
  const themeLabel = useId()
  const supported = 'Notification' in window
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported'>(supported ? Notification.permission : 'unsupported')
  const [minimum, setMinimum] = useState(String(settings?.polling_min_seconds ?? 10))
  const [maximum, setMaximum] = useState(String(settings?.polling_max_seconds ?? 120))
  const [cadenceError, setCadenceError] = useState('')
  const [cadenceSaved, setCadenceSaved] = useState(false)
  useEffect(() => {
    if (!settings) return
    setMinimum(String(settings.polling_min_seconds)); setMaximum(String(settings.polling_max_seconds))
  }, [settings?.polling_min_seconds, settings?.polling_max_seconds])
  useEffect(() => {if (!cadenceSaved) return; const t = setTimeout(() => setCadenceSaved(false), 2200); return () => clearTimeout(t)}, [cadenceSaved])

  if (!settings) return <div className="settings-view"><p className="settings-hint">正在读取设置…</p></div>

  const wantsNotifications = settings.notify_on_change || settings.notify_on_approval
  const setNotify = async (patch: Partial<AppSettings>) => {
    // Turning an alert on is the moment to ask the browser, while the click still counts as a user gesture.
    if (supported && Object.values(patch).some(Boolean) && Notification.permission === 'default') {
      setPermission(await Notification.requestPermission())
    }
    await save(patch).catch(() => {})
  }
  const dirty = minimum !== String(settings.polling_min_seconds) || maximum !== String(settings.polling_max_seconds)
  const saveCadence = async () => {
    const min = Number(minimum), max = Number(maximum)
    if (!Number.isInteger(min) || min < 2 || min > 3600) return setCadenceError('最短间隔应为 2 到 3600 秒的整数')
    if (!Number.isInteger(max) || max < min || max > 86400) return setCadenceError('最长间隔应不小于最短间隔，且不超过 86400 秒')
    setCadenceError('')
    try {await save({polling_min_seconds: min, polling_max_seconds: max}); setCadenceSaved(true)}
    catch (e) {setCadenceError(e instanceof Error ? e.message : '没有保存')}
  }
  const runtime = state?.runtime

  return <div className="settings-view">
    <header className="settings-head">
      <span className="eyebrow">MORNING</span>
      <h1>设置</h1>
      <p>外观、通知和新任务的节奏会立刻生效，并保存在这台电脑上。</p>
    </header>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {state?.pi && <GatewaySettings busyAgent={state.agent_busy} onReady={onReady}/>}
    {capabilities}

    <Group title="外观">
      <Row label="主题" labelId={themeLabel} hint={settings.theme === 'system' ? '随系统的浅色 / 深色外观自动切换' : undefined}>
        <div className="segmented" role="radiogroup" aria-labelledby={themeLabel}>
          {THEMES.map(({value, label, Icon}) => <button key={value} type="button" role="radio" aria-checked={settings.theme === value}
            className={settings.theme === value ? 'selected' : ''} onClick={() => void save({theme: value}).catch(() => {})}>
            <Icon size={13} aria-hidden="true"/>{label}</button>)}
        </div>
      </Row>
      <Hue hue={settings.orb_hue} save={save}/>
    </Group>

    <Group title="通知" hint="页面在前台时，状态条会直接显示；切到别处时用系统通知提醒你。">
      <Toggle label="检测到变化时" hint="驻守的内容发生变化" checked={settings.notify_on_change} onChange={v => void setNotify({notify_on_change: v})}/>
      <Toggle label="需要你批准时" hint="Agent 准备好操作，等你确认" checked={settings.notify_on_approval} onChange={v => void setNotify({notify_on_approval: v})}/>
      <Toggle label="提示音" hint="提醒时播放一声轻响" checked={settings.notify_sound} onChange={v => void save({notify_sound: v}).catch(() => {})}/>
      <Row label="系统通知权限" hint={permission === 'granted' ? '已允许' : permission === 'denied' ? '已被浏览器拒绝，需要在浏览器的网站设置里重新允许'
        : permission === 'unsupported' ? '这个浏览器不支持系统通知' : '尚未询问'}>
        {permission === 'default' && <button type="button" className="secondary-button" onClick={() => void Notification.requestPermission().then(setPermission)}>
          <Bell size={13}/>允许通知</button>}
        {permission === 'granted' && <button type="button" className="secondary-button" disabled={!wantsNotifications}
          onClick={() => new Notification('Morning', {body: '通知已经可以正常送达。', tag: 'pulse-test'})}>
          <Bell size={13}/>发送测试通知</button>}
        {permission === 'denied' && <BellOff size={15} aria-hidden="true" className="settings-muted-icon"/>}
      </Row>
    </Group>

    <Group title="新任务的检查节奏" hint="长期没有变化时逐步放慢到最长间隔，出现变化后回到最短间隔。已有任务在各自的设置里单独调整。">
      <form className="settings-cadence" onSubmit={e => {e.preventDefault(); void saveCadence()}}>
        <label className="field-label">最短间隔 · 秒<input type="number" inputMode="numeric" min="2" max="3600" required value={minimum} onChange={e => setMinimum(e.target.value)}/></label>
        <label className="field-label">最长间隔 · 秒<input type="number" inputMode="numeric" min="2" max="86400" required value={maximum} onChange={e => setMaximum(e.target.value)}/></label>
        <button className="secondary-button" disabled={!dirty}>保存</button>
      </form>
      {cadenceError && <p className="inline-error" role="alert">{cadenceError}</p>}
      {cadenceSaved && <p className="settings-saved" role="status">已保存，新任务会从这个节奏开始</p>}
      <details className="cadence-explanation"><summary>查看检查节奏的变化</summary><Cadence minSeconds={Number(minimum) || settings.polling_min_seconds} maxSeconds={Number(maximum) || settings.polling_max_seconds}/></details>
    </Group>

    <Group title="驻守任务" hint="每个任务的检查间隔和页面读取配置。">
      {tasks.length === 0 ? <Row label="还没有驻守中的任务"/> : tasks.map(t =>
        <Row key={t.task_id} label={t.label} hint={`${stateNames[t.state] || t.lifecycle} · 约 ${t.interval_seconds} 秒一次`}>
          <button type="button" className="secondary-button" aria-label={`${t.label}的设置`} onClick={() => onOpenWatch(t.task_id)}>设置</button>
        </Row>)}
    </Group>

    <Group title="后台扩展" hint="浏览器操作使用已选择的 Responses 网关。截图会发送给对应的模型服务；现有驻守仍遵循创建时确认的授权。">
      <dl className="settings-facts">
        <Fact label="任务理解"><Status ok={!!state?.understanding_available} yes="已配置" no="未配置"/></Fact>
        <Fact label="编程 Agent"><Status ok={!!state?.coding_available} yes="已配置" no="未配置"/></Fact>
        <Fact label="浏览器执行"><Status ok={!!runtime?.available} yes="运行中" no="未运行"/></Fact>
        <Fact label="操作模型"><Status ok={settings._model_configured} yes="已配置" no="未配置"/></Fact>
      </dl>
    </Group>

    <Group title="数据">
      <dl className="settings-facts">
        <Fact label="数据目录">{settings._data_dir ? <code>{settings._data_dir}</code> : '仅在内存中'}</Fact>
        {state?.workspace && <Fact label="工作区"><code>{state.workspace}</code></Fact>}
      </dl>
    </Group>
  </div>
}
