import {useEffect, useState} from 'react'
import {Check, ChevronRight, FileDown, GitBranch, LoaderCircle, Plus, RefreshCw} from 'lucide-react'
import {api} from './api'
import {Dialog} from './Dialog'
import {Disclosure} from './Motion'
import {Markdown} from './Markdown'
import type {PiInteraction, PiModel, PiSnapshot} from './types'
import {setPiSessionId} from './session-context'

export const piCommand = async<T,>(command: string, params: Record<string, unknown> = {}) => {const result=await api<T&{sessionId?:string}>('/pulse/pi',{command,params});if(['new_session','switch_session','fork','clone'].includes(command)&&result.sessionId)setPiSessionId(result.sessionId);return result}
export const piControl = <T,>(command: string, params: Record<string, unknown> = {}) => api<T>('/pulse/pi/control', {command, params})
export type PiSession = {id:string; path:string; cwd:string; name?:string; firstMessage:string; modified:string; messageCount:number;running?:boolean;attention?:boolean;loaded?:boolean}
type Resource = {name:string; description?:string; filePath?:string; path?:string}
type Resources = {skills:Resource[]; prompts:Resource[]; extensions:Resource[]; context_files:Resource[]; tools:Resource[]; active_tools:string[]; callable_tools:string[]; diagnostics:{message:string; path?:string}[]; packages?:{source:string; scope:string}[]}
type TreeNode = {entry:{id:string; type:string; message?:{role:string; content?:string | {type:string; text?:string}[]}}; children:TreeNode[]; label?:string}
type Provider = {id:string; name:string; api_key:boolean; oauth:boolean; status:{configured:boolean; source?:string}}
const tabs = [{id:'sessions', label:'会话'}, {id:'model', label:'模型与上下文'}, {id:'resources', label:'能力'}, {id:'auth', label:'Pi 账户'}]

export function NativePanel({pi, close, refresh, onInsert, onSession, initialTab = 'sessions', embedded = false}: {initialTab?:string; embedded?:boolean; pi:PiSnapshot; close:() => void; refresh:() => void; onInsert:(text:string) => void; onSession?:() => void}) {
  const [tab, setTab] = useState(initialTab), [busy, setBusy] = useState(''), [error, setError] = useState('')
  const [sessions, setSessions] = useState<PiSession[]>([]), [models, setModels] = useState<PiModel[]>([])
  const [levels, setLevels] = useState<string[]>([]), [resources, setResources] = useState<Resources | null>(null)
  const [providers, setProviders] = useState<Provider[]>([]), [provider, setProvider] = useState('')
  const [name, setName] = useState(pi.state?.sessionName || ''), [exported, setExported] = useState<{path:string; download_url:string} | null>(null)
  const [forks, setForks] = useState<{entryId:string; text:string}[] | null>(null)
  const [summary, setSummary] = useState(''), [feedback, setFeedback] = useState('')
  const [commands, setCommands] = useState<{name:string; description?:string; source:string}[]>([])
  const [query, setQuery] = useState('')
  const [packageSource, setPackageSource] = useState(''), [packageLocal, setPackageLocal] = useState(true)
  const [importPath, setImportPath] = useState(''), [tree, setTree] = useState<TreeNode[] | null>(null)
  const [summarizeBranch, setSummarizeBranch] = useState(true)
  const run = async <T,>(label: string, operation: () => Promise<T>): Promise<T | undefined> => {
    setBusy(label); setError(''); setFeedback('')
    try {const result = await operation(); refresh(); return result} catch (e) {setError(e instanceof Error ? e.message : '操作没有完成')}
    finally {setBusy('')}
  }
  const load = async () => {
    if (tab === 'sessions') {const result = await piControl<PiSession[]>('sessions'); setSessions(result)}
    if (tab === 'model') {
      setModels(await piControl<PiModel[]>('models'))
      if (pi.agent_ready) setLevels((await piCommand<{levels:string[]}>('get_available_thinking_levels')).levels)
    }
    if (tab === 'resources') {
      setResources(await piControl<Resources>('resources'))
      setCommands((await piCommand<{commands:typeof commands}>('get_commands')).commands)
    }
    if (tab === 'auth') {
      const list = await piControl<Provider[]>('providers'); setProviders(list)
      if (!provider) setProvider(list.find(p => p.status.configured)?.id || list[0]?.id || '')
    }
  }
  useEffect(() => {void run('load', load)}, [tab])
  const selectedProvider = providers.find(p => p.id === provider)
  const activeModel = pi.state?.model
  const changeSession = async (command:string, params:Record<string, unknown> = {}) => {
    const result = await run('session', () => piCommand<{cancelled?:boolean; text?:string}>(command, params))
    if (result?.cancelled) setFeedback('扩展取消了这次会话切换。')
    else if (result) {onSession?.(); if (result.text) onInsert(result.text); close()}
  }
  const content = <>
    {!embedded && <div className="pi-tabs" role="tablist" aria-label="Agent 功能">{tabs.map(item => <button role="tab" aria-selected={tab === item.id} key={item.id} onClick={() => setTab(item.id)}>{item.label}</button>)}</div>}
    <div className="pi-panel-body">
      {error && <p className="inline-error" role="alert">{error}</p>}
      {feedback && <p className="support-copy" role="status">{feedback}</p>}
      {busy && <p className="pi-progress" role="status"><LoaderCircle size={14}/>{busy === 'login' ? '正在进行 Pi 原生认证…' : busy === 'compact' ? '正在整理上下文…' : '正在处理…'}</p>}
      {tab === 'sessions' && <>
        <div className="section-heading"><button className="secondary-button" disabled={!!busy} onClick={() => void changeSession('new_session')}><Plus size={14}/>新建会话</button></div>
        {pi.agent_ready && <form className="pi-name-form" onSubmit={e => {e.preventDefault(); void run('name', async () => {await piCommand('set_session_name', {name}); await load()})}}>
          <label className="field-label">当前会话名称<input value={name} onChange={e => setName(e.target.value)} placeholder="给这段工作一个名字"/></label><button className="secondary-button" disabled={!!busy || !name.trim()}>保存名称</button></form>}
        <div className="pi-session-list">{sessions.map(session => <button className={`pi-session ${session.id === pi.state?.sessionId ? 'selected' : ''}`} key={session.path||session.id} disabled={!!busy} onClick={() => void changeSession('switch_session', {sessionPath:session.path,sessionId:session.id})}>
          <div><strong>{session.name || session.firstMessage?.slice(0, 65) || '未命名会话'}</strong><span>{session.messageCount} 条消息 · {new Date(session.modified).toLocaleString('zh-CN', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'})}</span><small>{session.cwd}</small></div>
          {session.id === pi.state?.sessionId ? <Check size={16}/> : <ChevronRight size={16}/>}</button>)}</div>
        {!sessions.length && !busy && <p className="pi-empty">暂无历史会话</p>}
        {pi.agent_ready && <Disclosure title={<> 分支与导出 </>}>
          <div className="pi-button-row"><button className="secondary-button" disabled={!!busy || pi.state?.isStreaming} onClick={() => void changeSession('clone')}><GitBranch size={14}/>复制为新分支</button>
            <button className="secondary-button" disabled={!!busy || pi.state?.isStreaming} onClick={() => void run('forks', async () => setForks((await piCommand<{messages:{entryId:string; text:string}[]}>('get_fork_messages')).messages))}>从历史消息分支</button>
            <button className="secondary-button" disabled={!!busy} onClick={() => void run('export', async () => setExported(await piCommand<{path:string; download_url:string}>('export_html')))}><FileDown size={14}/>导出 HTML</button></div>
          {exported && <p className="support-copy"><a className="secondary-button" href={exported.download_url} download>下载会话 HTML</a><br/>同时保存到 <code>{exported.path}</code></p>}
          {forks?.map(item => <button className="pi-command" key={item.entryId} onClick={() => void changeSession('fork', {entryId:item.entryId})}><GitBranch size={14}/><span>{item.text}</span></button>)}
          <button className="secondary-button" disabled={!!busy} onClick={() => void run('tree', async () => setTree((await piCommand<{tree:TreeNode[]}>('get_tree')).tree))}>查看完整会话树</button>
          {tree && <><label className="pi-check"><input type="checkbox" checked={summarizeBranch} onChange={e => setSummarizeBranch(e.target.checked)}/>切换前总结当前分支</label><SessionTree nodes={tree} busy={!!busy || !!pi.state?.isStreaming} select={id => void run('tree', async () => {const result = await piControl<{cancelled:boolean; editorText?:string}>('navigate_tree', {entryId:id, summarize:summarizeBranch}); if (!result.cancelled) {onSession?.(); if (result.editorText) onInsert(result.editorText); close()}})}/></>}
        </Disclosure>}
        <Disclosure title={<> 导入 Pi 会话 </>}><form className="pi-name-form" onSubmit={e => {e.preventDefault(); void run('import', async () => {const result = await piControl<{cancelled:boolean}>('import_session', {path:importPath}); if (!result.cancelled) {onSession?.(); close()}})}}><label className="field-label">本地 JSONL 文件路径<input value={importPath} onChange={e => setImportPath(e.target.value)} required/></label><button className="secondary-button" disabled={!!busy || !!pi.state?.isStreaming}>导入并继续</button></form></Disclosure>
      </>}
      {tab === 'model' && <>
        {!pi.agent_ready && <button className="primary-button" disabled={!!busy} onClick={() => void run('start', async () => {await piControl('start_agent'); setLevels((await piCommand<{levels:string[]}>('get_available_thinking_levels')).levels)})}>启动 Agent</button>}
        <label className="field-label">当前模型<select value={activeModel ? `${activeModel.provider}::${activeModel.id}` : ''} disabled={!!busy || !pi.agent_ready || pi.state?.isStreaming} onChange={e => {const [provider, modelId] = e.target.value.split('::'); void run('model', async () => {await piCommand('set_model', {provider, modelId}); setLevels((await piCommand<{levels:string[]}>('get_available_thinking_levels')).levels)})}}>
          <option value="" disabled>选择模型</option>{models.map(model => <option key={`${model.provider}::${model.id}`} value={`${model.provider}::${model.id}`}>{model.name} · {model.provider.startsWith('gateway-') ? 'Gateway' : model.provider}</option>)}</select></label>
        {activeModel && <p className="support-copy">上下文 {activeModel.contextWindow.toLocaleString()} tokens · 最大输出 {activeModel.maxTokens.toLocaleString()} · {activeModel.input.includes('image') ? '支持图片' : '文字输入'}</p>}
        {!!levels.length && <label className="field-label">思考深度<select value={pi.state?.thinkingLevel || 'off'} disabled={!!busy || pi.state?.isStreaming} onChange={e => void run('thinking', () => piCommand('set_thinking_level', {level:e.target.value}))}>{levels.map(level => <option key={level}>{level}</option>)}</select></label>}
        {pi.agent_ready && <>
          <div className="pi-context"><span>当前上下文</span><strong>{pi.stats?.contextUsage?.percent == null ? '等待模型返回用量' : `${pi.stats.contextUsage.percent.toFixed(1)}%`}</strong>
            <progress aria-label="上下文使用比例" max={100} value={pi.stats?.contextUsage?.percent || 0}/><small>{pi.stats?.contextUsage?.tokens?.toLocaleString() || '—'} / {pi.stats?.contextUsage?.contextWindow?.toLocaleString() || '—'} tokens</small></div>
          <label className="pi-check"><input type="checkbox" checked={!!pi.state?.autoCompactionEnabled} disabled={!!busy} onChange={e => void run('auto', () => piCommand('set_auto_compaction', {enabled:e.target.checked}))}/>Pi 自动整理上下文</label>
          <label className="field-label">整理时额外保留的重点<textarea rows={2} value={summary} onChange={e => setSummary(e.target.value)} placeholder="可选"/></label>
          <button className="secondary-button" disabled={!!busy || pi.state?.isStreaming} onClick={() => void run('compact', async () => {await piCommand('compact', {customInstructions:summary || undefined}); setFeedback('Pi 已完成上下文整理，完整会话记录仍然保留。')})}>立即整理上下文</button>
          <Disclosure title={<> 队列与重试 </>}><label className="field-label">处理中补充消息<select value={pi.state?.steeringMode} onChange={e => void run('queue', () => piCommand('set_steering_mode', {mode:e.target.value}))}><option value="one-at-a-time">逐条处理</option><option value="all">一次处理全部</option></select></label>
            <label className="field-label">后续消息<select value={pi.state?.followUpMode} onChange={e => void run('queue', () => piCommand('set_follow_up_mode', {mode:e.target.value}))}><option value="one-at-a-time">逐条处理</option><option value="all">一次处理全部</option></select></label>
            <div className="pi-button-row"><button className="secondary-button" onClick={() => void run('retry', () => piCommand('set_auto_retry', {enabled:true}))}>开启自动重试</button><button className="secondary-button" onClick={() => void run('retry', () => piCommand('set_auto_retry', {enabled:false}))}>关闭自动重试</button></div></Disclosure>
        </>}
      </>}
      {tab === 'resources' && resources && <>
        <div className="section-heading"><button className="secondary-button" disabled={!!busy || pi.state?.isStreaming} onClick={() => void run('reload', async () => {setResources(await piControl<Resources>('reload')); await load()})}><RefreshCw size={14}/>重新加载</button></div>
        <input aria-label="搜索能力" placeholder="搜索命令、Skills 或提示词…" value={query} onChange={e => setQuery(e.target.value)}/>
        <div className="pi-command-list">{commands.filter(command => `${command.name} ${command.description}`.toLowerCase().includes(query.toLowerCase())).map(command => <button className="pi-command" key={`${command.source}-${command.name}`} onClick={() => {onInsert(`/${command.name} `); close()}}>
          <span><strong>/{command.name}</strong><small>{command.description || command.source}</small></span><ChevronRight size={14}/></button>)}</div>
        {!commands.length && <p className="pi-empty">暂无自定义命令</p>}
        <Disclosure title={<> 工具 · {resources.tools.length} 项 </>}>{resources.tools.map(tool => <label className="pi-check" key={tool.name}><input type="checkbox" checked={resources.active_tools.includes(tool.name)} disabled={!!busy || pi.state?.isStreaming} onChange={e => void run('tools', async () => setResources(await piControl<Resources>('set_tools', {names:e.target.checked ? [...resources.active_tools, tool.name] : resources.active_tools.filter(name => name !== tool.name)})))}/><span>{tool.name}<small>{tool.description}</small></span></label>)}</Disclosure>
        {[{title:'Skills', rows:resources.skills}, {title:'提示词', rows:resources.prompts}, {title:'扩展', rows:resources.extensions}, {title:'工作区说明', rows:resources.context_files}].map(group => <Disclosure key={group.title} title={<> {group.title} · {group.rows.length} </>}>{group.rows.map((item, index) => <p className="pi-resource" key={index}><strong>{item.name}</strong><span>{item.description}</span><code>{item.path || item.filePath}</code></p>)}</Disclosure>)}
        {!!resources.diagnostics.length && <Disclosure title={<> 加载诊断 · {resources.diagnostics.length} </>}>{resources.diagnostics.map((diagnostic, index) => <p className="support-copy" key={index}>{diagnostic.message}<code>{diagnostic.path}</code></p>)}</Disclosure>}
        <Disclosure title={<> 管理 Pi 扩展包 </>}>
          <p className="support-copy">扩展包可以执行代码，请确认来源可信。</p>
          <form onSubmit={e => {e.preventDefault(); void run('package', async () => {setResources(await piControl<Resources>('package', {action:'install', source:packageSource, local:packageLocal})); setPackageSource(''); await load()})}}>
            <label className="field-label">包来源<input value={packageSource} onChange={e => setPackageSource(e.target.value)} required/></label>
            <label className="pi-check"><input type="checkbox" checked={packageLocal} onChange={e => setPackageLocal(e.target.checked)}/>仅用于当前工作区</label>
            <button className="secondary-button" disabled={!!busy || !!pi.state?.isStreaming}>安装并加载</button></form>
          {resources.packages?.map(item => <div className="pi-package" key={`${item.scope}:${item.source}`}><span><code>{item.source}</code><small>{item.scope === 'project' ? '工作区' : '用户目录'}</small></span><button className="text-button" disabled={!!busy || !!pi.state?.isStreaming} onClick={() => void run('package', async () => {setResources(await piControl<Resources>('package', {action:'update', source:item.source})); await load()})}>更新</button><button className="danger-link" disabled={!!busy || !!pi.state?.isStreaming} onClick={() => void run('package', async () => {setResources(await piControl<Resources>('package', {action:'remove', source:item.source, local:item.scope === 'project'})); await load()})}>移除</button></div>)}
        </Disclosure>

      </>}
      {tab === 'auth' && <>

        <label className="field-label">提供方<select value={provider} onChange={e => setProvider(e.target.value)}>{providers.map(item => <option key={item.id} value={item.id}>{item.name} {item.status.configured ? '· 已配置' : ''}</option>)}</select></label>
        <div className="pi-button-row">{selectedProvider?.api_key && <button className="secondary-button" disabled={!!busy} onClick={() => void run('login', async () => {await piControl('auth_login', {provider, authType:'api_key'}); await load()})}>配置 API Key</button>}
          {selectedProvider?.oauth && <button className="primary-button" disabled={!!busy} onClick={() => void run('login', async () => {await piControl('auth_login', {provider, authType:'oauth'}); await load()})}>账户登录</button>}
          {busy === 'login' && <button className="secondary-button" onClick={() => void piControl('auth_cancel')}>取消登录</button>}
          {selectedProvider?.status.configured && <button className="danger-link" disabled={!!busy} onClick={() => void run('logout', async () => {await piControl('auth_logout', {provider}); await load()})}>移除已保存的认证</button>}</div>
        {pi.notices?.slice(-3).map(notice => <div className="pi-auth-notice" key={notice.id}><p>{notice.message || notice.instructions}</p>{notice.url && <a href={notice.url} target="_blank" rel="noreferrer">打开登录页面</a>}{notice.verificationUri && <><a href={notice.verificationUri} target="_blank" rel="noreferrer">打开验证页面</a><code>{notice.userCode}</code></>}{notice.links?.map(link => <a key={link.url} href={link.url} target="_blank" rel="noreferrer">{link.label || '打开链接'}</a>)}</div>)}
      </>}
    </div>
  </>
  return embedded ? <div className="pi-panel-embedded">{content}</div> : <Dialog title="Agent 工作空间" close={close} wide>{content}</Dialog>
}

function SessionTree({nodes, select, busy}: {nodes:TreeNode[]; select:(id:string) => void; busy:boolean}) {
  return <ul className="pi-tree">{nodes.map(node => {
    const content = node.entry.message?.content
    const text = typeof content === 'string' ? content : content?.filter(part => part.type === 'text').map(part => part.text).join('')
    return <li key={node.entry.id}><button disabled={busy} onClick={() => select(node.entry.id)} title={text || node.entry.type}><span>{node.label || node.entry.message?.role || node.entry.type}</span>{text?.slice(0, 90) || node.entry.id}</button>{node.children.length > 0 && <SessionTree nodes={node.children} busy={busy} select={select}/>}</li>
  })}</ul>
}

export function NativeInteraction({request, refresh}: {request:PiInteraction; refresh:() => void}) {
  const [value, setValue] = useState(request.prefill || ''), [busy, setBusy] = useState(false), [error, setError] = useState('')
  const kind = request.kind || (request.method === 'auth_prompt' ? request.type : request.method)
  const answer = async (response:Record<string, unknown>) => {
    setBusy(true); setError('')
    try {await api('/pulse/pi/interaction', {id:request.id, answer:response}); setValue(''); refresh()}
    catch (e) {setError(e instanceof Error ? e.message : '没有提交'); setBusy(false)}
  }
  return <Dialog title={request.method === 'project_trust' ? '工作区信任' : request.title || 'Pi 需要你的输入'} close={() => {if (!busy) void answer({cancelled:true})}} eyebrow={request.method === 'auth_prompt' ? 'AUTHENTICATION' : 'PI'}>
    <div className="creator-body pi-interaction">
      {request.method === 'project_trust' && <p className="support-copy">Pi 将依据你的选择加载工作区的设置、Skills 和可执行扩展。</p>}
      <Markdown text={request.message || request.title || ''}/>
      {request.cwd && <code className="pi-path">{request.cwd}</code>}
      {kind === 'select' ? <div className="pi-select-options">{request.options?.map(option => {const label = typeof option === 'string' ? option : option.label; const selected = typeof option === 'string' ? option : option.id;
        return <button className="secondary-button" disabled={busy} key={selected} onClick={() => void answer({value:selected})}>{label}</button>})}</div>
        : kind === 'confirm' ? <div className="pi-button-row"><button className="primary-button" disabled={busy} onClick={() => void answer({confirmed:true})}>确认</button><button className="secondary-button" disabled={busy} onClick={() => void answer({confirmed:false})}>拒绝</button></div>
          : <form onSubmit={e => {e.preventDefault(); void answer({value})}}><label className="field-label">{kind === 'secret' ? '认证凭据' : '你的回答'}
            {kind === 'editor' ? <textarea rows={8} value={value} onChange={e => setValue(e.target.value)}/> : <input type={kind === 'secret' ? 'password' : 'text'} autoComplete="off" value={value} placeholder={request.placeholder} onChange={e => setValue(e.target.value)}/>}</label>
            <button className="primary-button" disabled={busy}>{busy ? '正在提交…' : '继续'}</button></form>}
      {error && <p className="inline-error" role="alert">{error}</p>}
    </div>
  </Dialog>
}
