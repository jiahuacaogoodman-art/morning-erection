import {useEffect, useState} from 'react'
import {CheckCheck, CornerUpLeft, RefreshCw, ShieldCheck} from 'lucide-react'
import {piControl} from './NativePi'
import {Disclosure} from './Motion'
import {Dialog} from './Dialog'
import {FieldHelp} from './FieldHelp'

export type PreferenceValue = string | number | boolean | string[] | null
export type PreferenceField = {key:string; group:string; label:string; type:'select'|'integer'|'boolean'|'text'|'list'; help:string;
  min?:number; unit?:string; advanced?:boolean; globalOnly?:boolean; applies?:string; options?:{value:string;label:string}[];
  value:PreferenceValue; defaultValue:PreferenceValue; stored:{global:PreferenceValue;project:PreferenceValue}; scopeValues:{global:PreferenceValue;project:PreferenceValue}; source:string}
type Preferences = {fields:PreferenceField[]; revisions:{global:string;project:string}; projectTrusted:boolean; workspace:string; runtimeReady:boolean; modelOverrides:Record<string,unknown>; diagnostics:{scope:string;message:string}[]; message?:string}
export const preferenceGroups:Record<string,{title:string;hint:string}> = {
  interaction:{title:'对话与消息队列',hint:'决定如何接收新指令、接续任务。'},
  context:{title:'上下文与分支总结',hint:'由 Pi 管理压缩与会话树，保留完整原始历史。'},
  retry:{title:'失败与重试',hint:'区分 Agent 轮次重试和提供商传输重试。'},
  transport:{title:'连接与缓存',hint:'传输方式、超时，以及提示缓存预热。'},
  images:{title:'图片输入',hint:'决定图片如何进入模型上下文。'},
  tools:{title:'工具与执行',hint:'原生工具、Skills、Codemode 与终端行为。'},
  resources:{title:'资源发现路径',hint:'扩展 Pi 原有的 Skills、Extensions 和提示词目录。'},
  trust:{title:'工作区信任',hint:'控制新工作区是否可以载入配置和扩展代码。'},
  budgets:{title:'模型思考预算',hint:'仅适用于支持显式思考预算的模型。'},
}

export function useNativePreferences(enabled:boolean) {
  const [data,setData] = useState<Preferences|null>(null), [scope,chooseScope] = useState<'global'|'project'>('global')
  const [draft,setDraft] = useState<Record<string,PreferenceValue>>({}), [busy,setBusy] = useState(false)
  const [error,setError] = useState(''), [notice,setNotice] = useState('')
  const load = async () => {
    setBusy(true);setError('')
    try {setData(await piControl<Preferences>('native_preferences'))} catch(e){setError((e as Error).message)} finally{setBusy(false)}
  }
  useEffect(()=>{if(enabled) void load()},[enabled])
  const dirty = Object.keys(draft).length
  const setScope = (next:'global'|'project') => {if(next===scope)return;if(dirty){setError('请先保存或放弃当前修改，再切换设置范围。');return}chooseScope(next);setError('');setNotice('')}
  const setField = (key:string,value:PreferenceValue) => {setDraft(current=>({...current,[key]:value}));setNotice('');setError('')}
  const value = (field:PreferenceField):PreferenceValue => Object.hasOwn(draft,field.key)
    ? draft[field.key] === null ? scope==='project' ? field.scopeValues.global : field.defaultValue : draft[field.key]
    : field.scopeValues[scope]
  const save = async () => {
    if(!data||!dirty)return
    setBusy(true);setError('');setNotice('')
    const patch=Object.fromEntries(Object.entries(draft).map(([key,value])=>[key,data.fields.find(field=>field.key===key)?.type==='list'&&typeof value==='string'?value.split('\n').map(line=>line.trim()).filter(Boolean):value]))
    try {const next=await piControl<Preferences>('update_native_preferences',{scope,patch,revision:data.revisions[scope],workspace:data.workspace});setData(next);setDraft({});setNotice(next.message||'Pi 设置已保存。')}
    catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  const discard = async () => {setDraft({});setNotice('');await load()}
  const trust = async (trusted:boolean) => {if(!data)return false;setBusy(true);setError('');try{setData(await piControl<Preferences>('set_workspace_trust',{workspace:data.workspace,trusted}));setNotice(trusted?'已通过 Pi 信任当前工作区。':'已取消当前工作区的信任。');return true}catch(e){setError((e as Error).message);return false}finally{setBusy(false)}}
  return {data,scope,setScope,draft,setField,value,dirty,busy,error,notice,load,save,discard,trust}
}
export type NativePreferenceController = ReturnType<typeof useNativePreferences>

function PreferenceRow({field,controller,disabled}:{field:PreferenceField;controller:NativePreferenceController;disabled:boolean}) {
  const current=controller.value(field), id='pref-'+field.key.replaceAll('.','-')
  const inherited=Object.hasOwn(controller.draft,field.key) ? controller.draft[field.key]===null : field.stored[controller.scope]===null
  const inactive=disabled||controller.scope==='project'&&!!field.globalOnly
  return <div className={`preference-row ${inactive?'is-disabled':''}`} id={'setting-'+field.key}>
    <div className="preference-copy"><div className="preference-title-row"><label htmlFor={id}>{field.label}{field.applies==='new_session'&&!field.label.includes('新会话')&&<span className="preference-tag">新会话</span>}</label>
      <small>{field.globalOnly?'仅用户设置':inherited?controller.scope==='project'?'继承用户设置或 Pi 默认':'使用 Pi 默认':'此范围已配置'}</small>{field.help&&<FieldHelp label={field.label}>{field.help}</FieldHelp>}</div></div>
    <div className="preference-control">
      {field.type==='boolean'?<button id={id} type="button" role="switch" aria-label={field.label} aria-checked={!!current} disabled={inactive} className="switch" onClick={()=>controller.setField(field.key,!current)}><span className="switch-knob"/></button>
        : field.type==='select'?<select id={id} value={String(current??'')} disabled={inactive} onChange={e=>controller.setField(field.key,e.target.value||null)}><option value="">由 Pi 决定</option>{field.options?.map(option=><option key={option.value} value={option.value}>{option.label}</option>)}</select>
        : field.type==='list'?<textarea id={id} rows={3} value={Array.isArray(current)?current.join('\n'):String(current??'')} disabled={inactive} onChange={e=>controller.setField(field.key,e.target.value)}/>
        : <div className="input-with-unit"><input id={id} type={field.type==='integer'?'number':'text'} min={field.min} step={field.type==='integer'?1:undefined} placeholder="由 Pi 决定" value={current===null?'':String(current)} disabled={inactive} onChange={e=>controller.setField(field.key,e.target.value===''?null:field.type==='integer'?Number(e.target.value):e.target.value)}/>{field.unit&&<span>{field.unit}</span>}</div>}
      <button type="button" className="inherit-button" aria-label={`${field.label}：恢复继承`} title="恢复继承" disabled={inactive||inherited} onClick={()=>controller.setField(field.key,null)}><CornerUpLeft size={13}/></button>
    </div>
  </div>
}

export function NativePreferences({controller,groups,busyAgent,focusKey}:{controller:NativePreferenceController;groups:string[];busyAgent?:boolean;focusKey?:string}) {
  const [trustPrompt,setTrustPrompt]=useState<boolean|null>(null)
  const [opened,setOpened]=useState<Record<string,boolean>>({})
  const {data,scope,busy}=controller
  useEffect(()=>{if(focusKey&&data){const group=data.fields.find(f=>f.key===focusKey)?.group;if(group)setOpened(value=>({...value,[group]:true}))}},[focusKey,data?.workspace])
  if(!data)return <div className="settings-loading" role="status">{controller.error||'正在读取 Pi 原生设置…'}{controller.error&&<button className="secondary-button" onClick={()=>void controller.load()}>重新读取</button>}</div>
  const disabled=busy||!!busyAgent||scope==='project'&&!data.projectTrusted
  return <div className="native-preferences">
    <div className="preference-scope"><div><strong>设置保存位置</strong><span>{scope==='global'?'所有工作区的默认设置':'仅影响当前工作区'}</span></div>
      <div className="segmented" role="group" aria-label="Pi 设置范围"><button className={scope==='global'?'selected':''} aria-pressed={scope==='global'} onClick={()=>controller.setScope('global')}>用户</button><button className={scope==='project'?'selected':''} aria-pressed={scope==='project'} onClick={()=>controller.setScope('project')}>当前工作区</button></div></div>
    {scope==='project'&&!data.projectTrusted&&<div className="settings-callout"><ShieldCheck size={18}/><div><strong>先确认这个工作区</strong><p>信任后，Pi 可以载入这里的配置、Skills 和可执行扩展。</p><code>{data.workspace}</code><button className="secondary-button" disabled={busy||busyAgent} onClick={()=>setTrustPrompt(true)}>检查并信任工作区</button></div></div>}
    {busyAgent&&<p className="settings-hint" role="status">当前任务完成或停止后，可以修改原生设置。</p>}
    {groups.map((group,index)=>{const fields=data.fields.filter(f=>f.group===group);const pending=fields.filter(f=>Object.hasOwn(controller.draft,f.key)).length;const definition=preferenceGroups[group];const groupOpen=opened[group]??index===0
      return <Disclosure key={group} className="preference-group" title={<>{definition.title}{pending>0&&<span className="setting-count">{pending} 项待保存</span>}</>} open={groupOpen} onOpenChange={open=>setOpened(value=>({...value,[group]:open}))}>
        {fields.filter(f=>!f.advanced||f.key===focusKey).map(field=><PreferenceRow field={field} key={field.key} controller={controller} disabled={disabled}/>)}
        {fields.some(f=>f.advanced&&f.key!==focusKey)&&<Disclosure className="preference-advanced" title="高级参数">{fields.filter(f=>f.advanced&&f.key!==focusKey).map(field=><PreferenceRow field={field} key={field.key} controller={controller} disabled={disabled}/>)}</Disclosure>}
        {group==='trust'&&data.projectTrusted&&<div className="trust-state"><CheckCheck size={15}/><span>当前工作区已通过 Pi 信任检查</span><button className="text-button" disabled={busy||busyAgent} onClick={()=>setTrustPrompt(false)}>管理信任</button></div>}
      </Disclosure>
    })}
    {groups.includes('context')&&Object.keys(data.modelOverrides).length>0&&<Disclosure title="现有模型专用覆盖" hint="这些值由原生 Pi 配置管理，会优先于通用上下文参数。"><pre className="settings-code">{JSON.stringify(data.modelOverrides,null,2)}</pre></Disclosure>}
    {data.diagnostics.map((item,i)=><p role="alert" className="inline-error" key={i}>{item.message}</p>)}
    <p className="native-settings-note"><button className="text-button" disabled={busy||!!controller.dirty} onClick={()=>void controller.load()}><RefreshCw size={12}/>重新读取</button></p>
    {trustPrompt!==null&&<Dialog title={trustPrompt?'信任这个工作区？':'取消工作区信任？'} close={()=>{if(!busy)setTrustPrompt(null)}}><div className="creator-body"><p>授权范围仅限当前工作区：</p><code className="pi-path">{data.workspace}</code><p>{trustPrompt?'Pi 将允许加载其中的设置、Skills 和扩展代码。请确认这个目录的来源可信。':'Pi 将停止加载这里的工作区设置与扩展。用户目录里的设置与会话记录仍保留。'}</p>{controller.error&&<p role="alert" className="inline-error">{controller.error}</p>}<div className="pi-button-row"><button className="primary-button" disabled={busy||busyAgent} onClick={()=>void controller.trust(trustPrompt).then(ok=>{if(ok)setTrustPrompt(null)})}>{trustPrompt?'信任并使用':'取消信任'}</button><button className="secondary-button" disabled={busy} onClick={()=>setTrustPrompt(null)}>返回</button></div></div></Dialog>}
  </div>
}
