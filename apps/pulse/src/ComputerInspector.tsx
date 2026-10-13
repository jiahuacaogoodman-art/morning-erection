import {useEffect,useRef,useState} from 'react'
import {Eye,Hand,Play,RefreshCw,ShieldCheck,ChevronDown} from 'lucide-react'
import {platformCommand,type PlatformState} from './Platform'
import type {WorkTarget} from './task-workspace'

type Permissions={observe_apps:string[];operate_apps:string[];screens:number[];paused:boolean;observe_all?:boolean;operate_all?:boolean;exclude_apps?:string[]}
type Inventory={apps:{id:string;name:string;running?:boolean;frontmost?:boolean}[];displays:{id:number;width:number;height:number}[];permissions:Permissions;display_error?:string;accessibility_permission?:boolean|null;screen_recording_permission?:boolean|null}
type Frame={id?:string;app?:string;display?:number;text?:string;window?:string;at?:number;image?:{mimeType:string;data:string}}
type Tile={id:string;name:string;frontmost:boolean;observable:boolean;window?:string;error?:string;sensitive?:boolean;summary?:string[]}

const imageSrc=(image:{mimeType:string;data:string})=>`data:${image.mimeType};base64,${image.data}`

export default function ComputerInspector({data,target,refresh}:{data:PlatformState;target:WorkTarget;refresh:()=>void}){
  const [device,setDevice]=useState(target.device||'local')
  const [app,setApp]=useState(target.app||'')
  const [inventory,setInventory]=useState<Inventory|null>(null)
  const [frame,setFrame]=useState<Frame|null>(null)
  const [busy,setBusy]=useState('')
  const [error,setError]=useState('')
  const [live]=useState(true)
  const [tiles,setTiles]=useState<Tile[]>([])
  const [showMore,setShowMore]=useState(false)
  const generation=useRef(0)
  const selectedTarget=useRef('');selectedTarget.current=device+':'+app
  const screen=app.startsWith('screen:')?Number(app.slice(7)):null
  const permissions=inventory?.permissions
  const allowed=!!permissions&&(screen!==null?!!permissions.observe_all||permissions.screens.includes(screen):!permissions.exclude_apps?.includes(app)&&(!!permissions.observe_all||permissions.observe_apps.includes(app)))
  const appName=tiles.find(t=>t.id===app)?.name||inventory?.apps.find(t=>t.id===app)?.name||app

  const load=async()=>{
    const version=++generation.current
    setBusy('inventory');setError('')
    try{const result=await platformCommand<Inventory>('inventory',{device_id:device});if(version===generation.current)setInventory(result)}
    catch(e){if(version===generation.current)setError((e as Error).message)}
    finally{if(version===generation.current)setBusy('')}
  }

  useEffect(()=>{setDevice(target.device||'local');if(target.app)setApp(target.app);else if(target.display)setApp('screen:'+target.display)},[target.device,target.app,target.display])
  useEffect(()=>{setInventory(null);setFrame(null);setTiles([]);if(data.modules.computer)void load();return()=>{generation.current++}},[device,data.modules.computer])
  useEffect(()=>{setFrame(null);setError('')},[app])

  // Overview polling — drives the window grid
  useEffect(()=>{
    if(!inventory||!data.modules.computer)return
    let stopped=false,timer:ReturnType<typeof setTimeout>
    const poll=async()=>{
      try{const r=await platformCommand<{windows:Tile[];at:number}>('overview',{device_id:device,limit:8});if(!stopped)setTiles(r.windows)}
      catch{}
      finally{if(!stopped)timer=setTimeout(poll,document.hidden?15000:app?6000:3000)}
    }
    void poll();return()=>{stopped=true;clearTimeout(timer)}
  },[inventory,device,!!app,data.modules.computer])

  // Frame polling for the selected app
  useEffect(()=>{
    if(!app)return
    let stopped=false,timer:ReturnType<typeof setTimeout>
    const poll=async()=>{
      try{
        const r=await platformCommand<Frame>(live&&allowed?(screen!==null?'screen':'observe'):'last_frame',{device_id:device,...(screen!==null?{display:screen}:{app})})
        if(!stopped&&(r.id||r.image))setFrame(r)
      }catch(e){if(!stopped&&live)setError((e as Error).message)}
      finally{if(!stopped)timer=setTimeout(poll,live?1800:2500)}
    }
    void poll();return()=>{stopped=true;clearTimeout(timer)}
  },[device,app,live,allowed])

  const selectApp=(id:string)=>{
    if(id===app){setApp('');return}
    setApp(id)
    void platformCommand('activate',{device_id:device,app:id}).catch(()=>{})
  }

  const control=async(operation:string)=>{
    setBusy(operation);setError('')
    try{await platformCommand(operation,{device_id:device});await load();refresh()}
    catch(e){setError((e as Error).message)}
    finally{setBusy('')}
  }

  const savePermissions=async(next:Permissions)=>{
    setBusy('permissions');setError('')
    try{await platformCommand('permissions',{device_id:device,permissions:{observe_apps:next.observe_apps,operate_apps:next.operate_apps,screens:next.screens,paused:next.paused,observe_all:!!next.observe_all,operate_all:!!next.operate_all}});await load();refresh()}
    catch(e){setError((e as Error).message)}
    finally{setBusy('')}
  }

  const authorize=(operate:boolean)=>{
    if(!permissions)return
    const old=permissions
    void savePermissions({...old,...(screen!==null?{screens:[...new Set([...old.screens,screen])]}:{observe_apps:[...new Set([...old.observe_apps,app])],operate_apps:operate?[...new Set([...old.operate_apps,app])]:old.operate_apps})})
  }

  const requestSysPerms=async(permission:'accessibility'|'screen_recording')=>{
    setBusy('sysperm');setError('')
    try{const r=await platformCommand<{accessibility_permission:boolean|null;screen_recording_permission:boolean|null}>('open_system_settings',{device_id:device,permission});setInventory(cur=>cur?{...cur,...r}:cur)}
    catch(e){setError((e as Error).message)}
    finally{setBusy('')}
  }

  if(!data.modules.computer)return(
    <div className="ci-root"><div className="work-empty"><p>电脑能力已关闭。</p><a href="#settings/background">在设置中启用</a></div></div>
  )

  // Tiles to show: grid of running apps; selected one expands inline
  const otherApps=inventory?.apps.filter(a=>!tiles.some(t=>t.id===a.id))||[]

  return(
    <div className="ci-root">
      {/* top bar: device + status */}
      <div className="ci-bar">
        {data.devices.length>1&&<select aria-label="设备" value={device} onChange={e=>setDevice(e.target.value)}>
          {data.devices.map(d=><option key={d.id} value={d.id}>{d.name}</option>)}
        </select>}
        <span className="ci-status">{busy==='inventory'?'连接中…':inventory?'已连接':<button className="text-button" onClick={()=>void load()}>连接设备</button>}</span>
        <button className="icon-button ci-refresh" disabled={!!busy} aria-label="刷新" title="刷新" onClick={()=>void load()}><RefreshCw size={12}/></button>
      </div>

      {/* permission warnings — compact inline chips */}
      {inventory&&(inventory.accessibility_permission===false||inventory.screen_recording_permission===false)&&(
        <div className="ci-perm-strip">
          {inventory.accessibility_permission===false&&<button className="ci-perm-chip" disabled={busy==='sysperm'} onClick={()=>void requestSysPerms('accessibility')}><ShieldCheck size={10}/>辅助功能权限</button>}
          {inventory.screen_recording_permission===false&&<button className="ci-perm-chip" disabled={busy==='sysperm'} onClick={()=>void requestSysPerms('screen_recording')}><Eye size={10}/>录屏权限</button>}
        </div>
      )}

      {error&&<p className="ci-error" role="alert">{error}<button onClick={()=>void load()}>重试</button></p>}

      {/* window grid */}
      {!!tiles.length&&<div className="ci-grid">
        {tiles.map(tile=>{
          const isActive=tile.id===app
          return(
            <div key={tile.id} className={`ci-tile${isActive?' ci-tile--active':''}${tile.frontmost?' ci-tile--front':''}`}>
              {/* tile header — always visible, click to activate */}
              <button className="ci-tile-header" onClick={()=>selectApp(tile.id)}>
                {tile.frontmost&&<span className="ci-front-dot" aria-label="前台"/>}
                <span className="ci-tile-name">{tile.name}</span>
                {tile.window&&tile.window!==tile.name&&<span className="ci-tile-win">{tile.window}</span>}
              </button>

              {/* expanded content — only for selected tile */}
              {isActive&&<div className="ci-tile-body">
                {!allowed&&permissions&&(
                  <div className="ci-auth">
                    <ShieldCheck size={13}/>
                    <span>尚未授权观察</span>
                    <button className="text-button" disabled={!!busy} onClick={()=>authorize(false)}>仅观察</button>
                    {screen===null&&<button className="text-button" disabled={!!busy} onClick={()=>authorize(true)}>允许操作</button>}
                  </div>
                )}
                {frame?.image&&<img className="ci-frame-img" src={imageSrc(frame.image)} alt={frame.window||appName}/>}
                {frame?.text&&!frame.image&&<pre className="ci-frame-text">{frame.text}</pre>}
                {!frame&&allowed&&<div className="ci-frame-empty"><Eye size={16}/><span>{busy==='observe'?'观察中…':'等待画面'}</span></div>}
                {frame?.at&&<time className="ci-frame-time">{new Date(frame.at*1000).toLocaleTimeString('zh-CN')}</time>}
              </div>}
            </div>
          )
        })}
      </div>}

      {/* "more apps" dropdown for non-overview apps */}
      {inventory&&!!otherApps.length&&<div className="ci-more">
        <button className="ci-more-toggle text-button" onClick={()=>setShowMore(v=>!v)}>
          <ChevronDown size={11} style={{transform:showMore?'rotate(180deg)':'none',transition:'transform .15s'}}/>
          {showMore?'收起':'更多应用'} ({otherApps.length})
        </button>
        {showMore&&<div className="ci-more-list">
          {otherApps.map(a=><button key={a.id} className={`ci-more-item${a.id===app?' active':''}`} onClick={()=>selectApp(a.id)}>{a.name}</button>)}
          {inventory.displays.map(d=><button key={'screen:'+d.id} className={`ci-more-item${'screen:'+d.id===app?' active':''}`} onClick={()=>selectApp('screen:'+d.id)}>显示器 {d.id} · {d.width}×{d.height}</button>)}
        </div>}
      </div>}

      {/* takeover — subtle, at the bottom */}
      {inventory&&<div className="ci-takeover">
        <button className="ci-takeover-btn" disabled={!!busy} onClick={()=>void control(inventory.permissions.paused?'resume_computer':'takeover')}>
          {inventory.permissions.paused?<><Play size={11}/>交还 Agent</>:<><Hand size={11}/>接管</>}
        </button>
        {inventory.permissions.paused&&<span className="ci-paused-label">已接管</span>}
      </div>}
    </div>
  )
}
