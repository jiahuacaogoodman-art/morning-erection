import {useEffect,useRef,useState} from 'react'
import {Eye,Hand,Play,RefreshCw,ShieldCheck} from 'lucide-react'
import {platformCommand,type ComputerJob,type PlatformState} from './Platform'
import type {WorkTarget} from './task-workspace'
import {Disclosure} from './Motion'

type Permissions={observe_apps:string[];operate_apps:string[];screens:number[];paused:boolean}
type Inventory={apps:{id:string;name:string}[];displays:{id:number;width:number;height:number}[];permissions:Permissions;display_error?:string}
type Frame={id?:string;app?:string;display?:number;text?:string;window?:string;at?:number;image?:{mimeType:string;data:string}}

export default function ComputerInspector({data,target,jobs,refresh}:{data:PlatformState;target:WorkTarget;jobs:ComputerJob[];refresh:()=>void}){
  const [device,setDevice]=useState(target.device||'local'),[app,setApp]=useState(target.app||''),[inventory,setInventory]=useState<Inventory|null>(null),[frame,setFrame]=useState<Frame|null>(null)
  const [busy,setBusy]=useState(''),[error,setError]=useState(''),[live,setLive]=useState(false),[grant,setGrant]=useState(false)
  const generation=useRef(0)
  const selectedTarget=useRef('');selectedTarget.current=device+':'+app
  const screen=app.startsWith('screen:')?Number(app.slice(7)):null
  const allowed=!!inventory&&(screen!==null?inventory.permissions.screens.includes(screen):inventory.permissions.observe_apps.includes(app))
  const load=async()=>{const version=++generation.current;setBusy('inventory');setError('');try{const result=await platformCommand<Inventory>('inventory',{device_id:device});if(version===generation.current){setInventory(result);setApp(current=>current||result.permissions.observe_apps[0]||'')}}catch(e){if(version===generation.current)setError((e as Error).message)}finally{if(version===generation.current)setBusy('')}}
  useEffect(()=>{setDevice(target.device||'local');if(target.app)setApp(target.app);else if(target.display)setApp('screen:'+target.display)},[target.device,target.app,target.display])
  useEffect(()=>{setInventory(null);setFrame(null);setLive(false);if(target.app||target.id)void load();return()=>{generation.current++}},[device])
  useEffect(()=>{setFrame(null);setGrant(false);setError('')},[app])
  const observe=async()=>{if(!app)return;setBusy('observe');setError('');const version=generation.current,selection=selectedTarget.current;try{const result=await platformCommand<Frame>(screen!==null?'screen':'observe',{device_id:device,...(screen!==null?{display:screen}:{app})});if(version===generation.current&&selection===selectedTarget.current)setFrame(result)}catch(e){if(version===generation.current&&selection===selectedTarget.current)setError((e as Error).message)}finally{if(version===generation.current)setBusy('')}}
  useEffect(()=>{
    if(!app)return
    let stopped=false,timer:ReturnType<typeof setTimeout>
    const poll=async()=>{try{const result=await platformCommand<Frame>(live&&allowed?(screen!==null?'screen':'observe'):'last_frame',{device_id:device,...(screen!==null?{display:screen}:{app})});if(!stopped&&(result.id||result.image))setFrame(result)}catch(e){if(!stopped&&live){setError((e as Error).message);setLive(false)}}finally{if(!stopped)timer=setTimeout(poll,live?1800:2500)}}
    void poll();return()=>{stopped=true;clearTimeout(timer)}
  },[device,app,live,allowed])
  const control=async(operation:string)=>{setBusy(operation);setError('');try{await platformCommand(operation,{device_id:device});await load();refresh()}catch(e){setError((e as Error).message)}finally{setBusy('')}}
  const authorize=async(operate:boolean)=>{
    if(!inventory)return;setBusy('permissions');setError('')
    const old=inventory.permissions,permissions={...old,...(screen!==null?{screens:[...new Set([...old.screens,screen])]}:{observe_apps:[...new Set([...old.observe_apps,app])],operate_apps:operate?[...new Set([...old.operate_apps,app])]:old.operate_apps})}
    try{await platformCommand('permissions',{device_id:device,permissions:{observe_apps:permissions.observe_apps,operate_apps:permissions.operate_apps,screens:permissions.screens,paused:permissions.paused}});setGrant(false);await load();refresh()}catch(e){setError((e as Error).message)}finally{setBusy('')}
  }
  const relevant=jobs.filter(job=>job.device_id===device&&(!app||job.app===app))
  return <div className="task-computer-pane">
    <div className="computer-target"><select aria-label="任务执行设备" value={device} onChange={event=>setDevice(event.target.value)}>{data.devices.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}</select><button type="button" className="icon-button" disabled={!!busy} aria-label="重新读取设备" title="重新读取设备" onClick={()=>void load()}><RefreshCw size={13}/></button><span className="target-status">{busy==='inventory'?'正在连接':inventory?'已连接':<button type="button" className="text-button" disabled={!!busy} onClick={()=>void load()}>连接设备</button>}</span></div>
    {!data.modules.computer?<div className="work-empty"><p>电脑能力已关闭。</p><a href="#settings/background">在设置中启用</a></div>:<>
      {inventory&&<select className="computer-app-select" aria-label="任务中的应用或屏幕" value={app} onChange={event=>setApp(event.target.value)}><option value="">选择需要观察的应用</option>{target.app&&!inventory.apps.some(item=>item.id===target.app)&&<option value={target.app}>{target.app}</option>}{inventory.apps.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}{inventory.displays.map(item=><option key={'screen:'+item.id} value={'screen:'+item.id}>显示器 {item.id} · {item.width}×{item.height}</option>)}</select>}
      {error&&<p className="work-error" role="alert">{error}<button type="button" onClick={()=>void load()}>重试连接</button></p>}
      {app&&!allowed&&inventory&&<div className="computer-permission-note"><ShieldCheck size={15}/><div><strong>此任务尚未获得观察权限</strong><p>{inventory.apps.find(item=>item.id===app)?.name||app}</p><button type="button" className="text-button" onClick={()=>setGrant(value=>!value)}>选择授权范围</button>{grant&&<div className="computer-grant"><p>仅授权所选目标。Agent 读取的画面会进入当前模型上下文；本地预览不会发送给模型。</p><button type="button" disabled={!!busy} className="secondary-button" onClick={()=>void authorize(false)}>仅允许观察</button>{screen===null&&<button type="button" disabled={!!busy} className="secondary-button" onClick={()=>void authorize(true)}>允许操作，逐次确认</button>}</div>}</div></div>}
      {screen!==null&&inventory?.display_error&&<p className="work-error" role="status">{inventory.display_error}</p>}
      <div className="computer-frame" aria-label="任务电脑画面">{frame?.image?<img src={`data:${frame.image.mimeType};base64,${frame.image.data}`} alt={frame.window||'当前任务观察到的电脑画面'}/>:frame?.text?<pre>{frame.text}</pre>:<div className="work-empty"><Eye size={22}/><p>{app?(allowed?'暂无画面':'尚未授权'):'选择应用'}</p></div>}</div>
      <div className="computer-observation-bar"><button type="button" className="text-button" disabled={!!busy||!allowed} onClick={()=>void observe()}><Eye size={13}/>{busy==='observe'?'正在观察':'观察'}</button><label><input type="checkbox" checked={live} disabled={!allowed} onChange={event=>setLive(event.target.checked)}/>连续预览</label><time>{frame?.at?new Date(frame.at*1000).toLocaleTimeString('zh-CN'):'尚未观察'}</time></div>
      {frame?.image&&frame.text&&<Disclosure title="观察内容"><pre className="computer-observation-text">{frame.text}</pre></Disclosure>}
      {inventory&&<div className="computer-takeover"><button type="button" className="secondary-button" disabled={!!busy} onClick={()=>void control(inventory.permissions.paused?'resume_computer':'takeover')}>{inventory.permissions.paused?<Play size={13}/>:<Hand size={13}/>} {inventory.permissions.paused?'交还 Agent':'暂停并接管'}</button>{inventory.permissions.paused&&<span>已暂停</span>}</div>}
      {!!relevant.length&&<div className="computer-step-list" aria-label="当前任务电脑步骤">{relevant.slice(0,6).map(job=><div key={job.id}><i className={`resource-dot ${job.state}`}/><span>{job.goal}</span><small>{({queued:'等待设备',approval:'待批准',running:'执行中',observed:'待核验',failed:'未执行',unknown:'结果待核验',rejected:'已拒绝',cancelled:'已停止',expired:'已过期'} as Record<string,string>)[job.state]||job.state}</small></div>)}</div>}
    </>}
  </div>
}
