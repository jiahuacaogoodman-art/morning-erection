import {useEffect, useState} from 'react'
import {Check, KeyRound, PlugZap, Plus, RefreshCw, ShieldCheck} from 'lucide-react'
import {api, ApiError} from './api'
import {Dialog} from './Dialog'
import type {Gateway, GatewayList} from './types'

const empty = {name:'', base_url:'', api:'openai-responses', model:'', request_format:'native'}

function testMessage(item: Gateway) {
  if (item.credential_state === 'unavailable') return '无法读取系统凭据，请检查钥匙串权限'
  if (!item.has_credentials) return '尚未配置凭据'
  const test = item.connection_test
  if (test?.state === 'running') return '正在进行真实调用…'
  if (test && !test.current) return '配置已更新 · 请重新测试'
  if (test && test.state !== 'succeeded') return test.message || '测试未完成，请重新测试'
  if (item.last_test?.ok) return `上次调用成功 · ${((item.last_test.latency_ms || 0) / 1000).toFixed(1)} 秒`
  return item.last_test?.message || '凭据已保存 · 尚未测试'
}

export function GatewaySettings({busyAgent, onReady}: {busyAgent?: boolean; onReady?: () => void}) {
  const [data, setData] = useState<GatewayList | null>(null)
  const [editing, setEditing] = useState<Gateway | 'new' | null>(null)
  const [editRevision, setEditRevision] = useState<number | undefined>()
  const [form, setForm] = useState(empty), [key, setKey] = useState(''), [options, setOptions] = useState('{}')
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [loadError, setLoadError] = useState('')
  const [feedback, setFeedback] = useState(''), [refresh, setRefresh] = useState(0), [conflict, setConflict] = useState(false)
  const [confirm, setConfirm] = useState<'delete' | 'delete_credentials' | null>(null)
  useEffect(() => {
    let stopped = false, timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    const load = async () => {
      let testing = false
      try {
        const next = await api<GatewayList>('/pulse/gateways', undefined, AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]))
        testing = next.items.some(item => item.connection_test?.state === 'running')
        if (!stopped) {setData(next); setLoadError('')}
      } catch {if (!stopped) setLoadError('连接状态暂时无法更新，下面显示的是上次读取结果。')}
      finally {if (!stopped) timer = setTimeout(load, document.hidden ? 10000 : testing ? 700 : 4000)}
    }
    void load()
    const reload = () => setRefresh(value => value + 1)
    window.addEventListener('focus', reload)
    return () => {stopped = true; controller.abort(); clearTimeout(timer); window.removeEventListener('focus', reload)}
  }, [refresh])

  const edit = (item: Gateway | 'new') => {
    setError(''); setConflict(false); setConfirm(null); setKey(''); setEditing(item); setEditRevision(data?.revision)
    setForm(item === 'new' ? empty : {name:item.name, base_url:item.base_url, api:item.api, model:item.model, request_format:item.request_format || 'native'})
    setOptions(JSON.stringify(item === 'new' ? {} : item.model_options || {}, null, 2))
  }
  const action = async (body: Record<string, unknown>, label: string) => {
    setBusy(label); setError(''); setFeedback(''); setConflict(false)
    try {
      const result = await api<GatewayList & {message?:string}>('/pulse/gateways', body)
      setData(result); setFeedback(result.message || ''); setRefresh(value => value + 1)
      return result
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作没有完成')
      if (e instanceof ApiError && e.code === 'configuration_conflict') {setConflict(true); setRefresh(value => value + 1)}
      throw e
    } finally {setBusy('')}
  }
  const save = async () => {
    let model_options
    try {model_options = JSON.parse(options)} catch {setError('模型能力配置需要是有效 JSON。'); return}
    try {
      await action({...form, model_options, expected_revision:editRevision, ...(editing !== 'new' && editing ? {id:editing.id} : {}), ...(key ? {api_key:key} : {}), action:'save'}, 'save')
      setKey(''); setEditing(null)
    } catch { /* keep user input available for correction */ }
  }
  const start = async () => {
    setBusy('start'); setError('')
    try {await api('/pulse/pi/control', {command:'start_agent'}); setRefresh(value => value + 1); location.hash = 'chat'; onReady?.()}
    catch (e) {setError(e instanceof Error ? e.message : '启动没有完成')}
    finally {setBusy('')}
  }
  const runtime = data?.runtime
  return <section className="settings-group gateway-section" aria-labelledby="gateway-heading">
    <div className="section-heading"><div><h2 id="gateway-heading" className="sr-only">模型网关</h2></div>
      <button className="secondary-button" disabled={!!busy || busyAgent} onClick={() => edit('new')}><Plus size={14}/>添加 Gateway</button></div>
    {!data && !loadError && <p role="status">正在读取连接配置…</p>}
    {loadError && <p role="alert" className="inline-error">{loadError} <button className="text-button" onClick={() => setRefresh(value => value + 1)}>重新读取</button></p>}
    {runtime?.selected && <p className="settings-hint">当前会话：<strong>{runtime.selected.id}</strong> · {data?.items.find(item => item.provider_id === runtime.selected?.provider)?.name || runtime.selected.provider}</p>}
    {runtime?.selectionError && <p role="alert" className="inline-error">{runtime.selectionError}</p>}
    {runtime && ['pending','unavailable'].includes(runtime.state) && <div className="gateway-apply-state" role="status">
      <p>{runtime.message || '配置已保存，尚未应用到 Pi。'}</p>
      <button className="secondary-button" disabled={!!busy || busyAgent} onClick={() => void action({action:'apply'}, 'apply').catch(() => {})}><RefreshCw size={14}/>重新应用连接</button>
    </div>}
    {data?.items.length === 0 && <div className="gateway-empty"><KeyRound size={25}/><strong>先连接你的模型服务</strong></div>}
    <div className="gateway-list">{data?.items.map(item => {
      const testing = item.connection_test?.state === 'running'
      const selected = runtime?.selected?.provider === item.provider_id
      const verified = item.has_credentials && (item.connection_test ? item.connection_test.current && item.connection_test.state === 'succeeded' : item.last_test?.ok)
      const failed = item.connection_test ? item.connection_test.current && item.connection_test.state === 'failed' : item.last_test?.ok === false
      return <article key={item.id} className={`gateway-card ${selected ? 'active' : ''}`}>
        <div className="gateway-card-head"><span className="gateway-icon"><PlugZap size={18}/></span><div><strong>{item.name}</strong><span>{item.model}</span></div>
          {selected ? <span className="gateway-current"><Check size={12}/>当前会话</span> : data.active_id === item.id && <span className="gateway-current">新会话默认</span>}</div>
        <span className="gateway-address">{item.base_url}</span>
        <div className="gateway-status" role="status"><span className={`connection-dot ${!item.has_credentials || failed ? 'offline' : ''}`} style={!verified && !failed && item.has_credentials ? {background:'var(--ink-faint)'} : undefined}/><span>{testMessage(item)}</span>
          {item.last_test?.tested_at && <time title={new Date(item.last_test.tested_at).toLocaleString()}>{new Date(item.last_test.tested_at).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'})}</time>}</div>
        {data.test_errors?.[item.id] && <p className="inline-error" role="alert">{data.test_errors[item.id]}</p>}
        <div className="gateway-actions">
          {testing ? <button className="secondary-button" disabled={!!busy} onClick={() => void action({action:'cancel_test', test_id:item.connection_test!.id}, 'cancel').catch(() => {})}>取消测试</button>
            : <button className="secondary-button" title="发送一次简短的模型请求验证连接" disabled={!!busy || !item.has_credentials || !!loadError} onClick={() => void action({action:'test', id:item.id}, item.id).catch(() => {})}>测试连接</button>}
          <button className="text-button" disabled={!!busy || busyAgent} onClick={() => edit(item)}>编辑</button>
          {(!selected || data.active_id !== item.id) && <button className="text-button" disabled={!!busy || busyAgent || !item.has_credentials || !!loadError} onClick={() => void action({action:'activate', id:item.id, expected_revision:data.revision}, 'activate').catch(() => {})}>切换到此连接</button>}
        </div>
      </article>
    })}</div>
    {!editing && error && <p className="inline-error" role="alert">{error}</p>}
    {feedback && <p className="gateway-feedback" role="status"><ShieldCheck size={15}/>{feedback}</p>}
    {data?.items.some(item => item.id === data.active_id && item.has_credentials) && <button className="primary-button gateway-start" disabled={!!busy || busyAgent || !!runtime?.selectionError} onClick={() => void start()}>{busy === 'start' ? '正在启动 Pi…' : runtime?.agentReady ? '返回 Agent 对话' : '启动 Agent · 开始对话'}</button>}
    {busyAgent && <p className="settings-hint">当前任务完成或停止后，可以修改连接。连接测试不影响正在进行的对话。</p>}
    {editing && <Dialog title={editing === 'new' ? '添加模型网关' : '编辑模型网关'} close={() => {if (!busy) {setEditing(null); setKey(''); setConfirm(null)}}}>
      <form className="creator-body gateway-form" onSubmit={e => {e.preventDefault(); void save()}}>
        <label className="field-label">名称<input autoComplete="off" required value={form.name} onChange={e => setForm({...form, name:e.target.value})}/></label>
        <label className="field-label">API 根地址<input type="url" autoComplete="off" required placeholder="https://…/v1" value={form.base_url} onChange={e => setForm({...form, base_url:e.target.value})}/></label>
        <div className="condition-form"><label className="field-label">协议<select value={form.api} onChange={e => setForm({...form, api:e.target.value, request_format:'native'})}>
          <option value="openai-responses">OpenAI Responses</option><option value="openai-completions">OpenAI Chat Completions</option><option value="anthropic-messages">Anthropic Messages</option></select></label>
          <label className="field-label">模型名称<input required autoComplete="off" value={form.model} onChange={e => setForm({...form, model:e.target.value})}/></label></div>
        <label className="field-label">API Key / Bearer Token<input type="password" autoComplete="new-password" spellCheck={false} required={editing === 'new' || !editing.has_credentials} value={key}
          placeholder={editing !== 'new' && editing.has_credentials ? '已安全保存；留空则保留' : '输入认证凭据'} onChange={e => setKey(e.target.value)}/></label>
        <details className="advanced"><summary>兼容性与模型能力</summary>
          {form.api === 'openai-responses' && <label className="field-label">请求格式<select value={form.request_format} onChange={e => setForm({...form, request_format:e.target.value})}>
            <option value="native">Pi 原生格式</option><option value="instructions">Codex 网关兼容（instructions）</option></select></label>}
          <p className="support-copy">默认继承 Pi 的模型信息。自定义模型可填写 reasoning、input、contextWindow、maxTokens；空对象使用 Pi 默认值。</p>
          <textarea aria-label="模型能力 JSON" className="code-input" rows={5} value={options} onChange={e => setOptions(e.target.value)}/>
        </details>
        {error && <p className="inline-error" role="alert">{error}</p>}
        {conflict && <button type="button" className="secondary-button" onClick={() => {const latest = data?.items.find(item => editing !== 'new' && item.id === editing.id); if (latest) edit(latest); else {setError('该配置已被移除，请关闭编辑窗口。'); setRefresh(value => value + 1)}}}>放弃本次输入，载入最新配置</button>}
        {!confirm && <button className="primary-button full" disabled={!!busy || conflict}>{busy === 'save' ? '正在保存…' : '安全保存'}</button>}
        {editing !== 'new' && <div className="gateway-delete-actions">
          {confirm ? <div role="group" aria-label="确认移除连接">
            <p>{confirm === 'delete' ? `移除“${editing.name}”及其凭据？` : `删除“${editing.name}”的认证凭据？`}当前会话历史会保留，后续调用需要重新选择或配置连接。</p>
            <button type="button" className="danger-link" disabled={!!busy} onClick={() => void action({action:confirm, id:editing.id, expected_revision:editRevision}, 'delete').then(() => {setEditing(null); setKey(''); setConfirm(null)}).catch(() => {})}>确认移除</button>
            <button type="button" className="text-button" disabled={!!busy} onClick={() => setConfirm(null)}>保留</button>
          </div> : <>
            <button type="button" className="danger-link" disabled={!!busy || !editing.has_credentials} onClick={() => setConfirm('delete_credentials')}>删除凭据，保留配置</button>
            <button type="button" className="danger-link" disabled={!!busy} onClick={() => setConfirm('delete')}>移除 Gateway</button>
          </>}
        </div>}
      </form>
    </Dialog>}
  </section>
}
