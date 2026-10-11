import {useRef} from 'react'
import type {RefObject} from 'react'
import {AlertCircle, ChevronRight, Eye, FolderOpen, History, Laptop, ListTodo, MessageSquare, Monitor, Settings, SquarePen} from 'lucide-react'
import {MorningMark} from './Brand'
import {Disclosure, gsap, useGSAP, useReducedMotion} from './Motion'
import type {PiSession} from './NativePi'
import type {Watch} from './types'

type Props = {navigationRef:RefObject<HTMLElement|null>; expanded:boolean; mobile:boolean; mobileOpen:boolean; view:string;
  platformAvailable:boolean; hasPi:boolean; sessions:PiSession[]; sessionId?:string; tasks:Watch[]; selectedId?:string; workspace?:string|null;
  busy:boolean; connected:boolean; error:string; onNew:()=>void; onView:(view:string)=>void; onSession:(session:PiSession)=>void; onWatch:(watch:Watch)=>void; onHistory:()=>void}
export function Sidebar({navigationRef,expanded,mobile,mobileOpen,view,platformAvailable,hasPi,sessions,sessionId,tasks,selectedId,workspace,busy,connected,error,onNew,onView,onSession,onWatch,onHistory}:Props) {
  const reduced=useReducedMotion(), first=useRef(true), activeRail=useRef<HTMLDivElement>(null), mainNav=useRef<HTMLElement>(null)
  const collapsed=!expanded&&!mobile
  useGSAP(()=>{
    const element=navigationRef.current
    if(!element)return
    const duration=first.current||reduced?0:.38
    first.current=false
    gsap.to(element,{width:mobile?272:expanded?212:64,x:mobile&&!mobileOpen?-280:0,duration,ease:'power3.inOut',overwrite:true})
    gsap.to(element.querySelectorAll('.nav-label,.sidebar-wordmark'),{opacity:collapsed?0:1,duration:duration? .16:0,delay:collapsed?0:.05,overwrite:true})
  },{scope:navigationRef,dependencies:[expanded,mobile,mobileOpen,reduced]})
  useGSAP(()=>{
    const target=mainNav.current?.querySelector<HTMLElement>('.nav-item.selected')
    if(!activeRail.current||!target){if(activeRail.current)gsap.to(activeRail.current,{opacity:0,duration:.1});return}
    gsap.to(activeRail.current,{y:target.offsetTop,height:target.offsetHeight,opacity:1,duration:reduced?0:.3,ease:'power3.inOut',overwrite:true})
  },{scope:mainNav,dependencies:[view,expanded,mobile,reduced]})
  const nav=[{id:'chat',label:'Agent 对话',Icon:MessageSquare},...(platformAvailable?[{id:'computer',label:'Computer',Icon:Monitor},{id:'watch',label:'Watch',Icon:Eye},{id:'tasks',label:'Tasks',Icon:ListTodo},{id:'devices',label:'Devices',Icon:Laptop}]:[])]
  const tip=(label:string)=>collapsed?<span className="rail-tooltip" aria-hidden="true">{label}</span>:null
  return <aside ref={navigationRef} className={`sidebar ${mobileOpen?'mobile-open':''}`} data-collapsed={collapsed} inert={mobile&&!mobileOpen} aria-label="任务导航">
    <a className="brand" href="/#chat" aria-label="Morning 首页"><span className="brand-mark"><MorningMark size={35}/></span><span className="sidebar-wordmark">morning<span className="brand-period">.</span></span>{tip('Morning')}</a>
    <button className="compose-nav" aria-label={hasPi?'新建会话':'写消息'} disabled={hasPi&&busy} onClick={onNew}><SquarePen size={17}/><span className="nav-label">{hasPi?'新建会话':'写消息'}</span>{tip(hasPi?'新建会话':'写消息')}</button>
    <nav ref={mainNav} className="sidebar-primary" aria-label="工作空间"><div ref={activeRail} className="nav-gliding-highlight" aria-hidden="true"/>{nav.map(item=><button key={item.id} className={`nav-item ${view===item.id?'selected':''}`} aria-label={item.label} aria-current={view===item.id?'page':undefined} onClick={()=>onView(item.id)}><item.Icon size={18}/><span className="nav-label">{item.label}</span>{tip(item.label)}</button>)}
      {hasPi&&<button className="nav-item history-nav" aria-label="全部会话" onClick={onHistory}><History size={18}/><span className="nav-label">全部会话</span>{tip('全部会话')}</button>}
    </nav>
    <div className="sidebar-secondary" inert={collapsed} aria-hidden={collapsed}>
      {hasPi&&<Disclosure title={<>最近会话 <span>{sessions.length}</span></>} defaultOpen><div className="session-nav">{sessions.slice(0,5).map(session=><button key={session.id} className={session.id===sessionId?'selected':''} disabled={busy} title={session.name||session.firstMessage} onClick={()=>onSession(session)}><MessageSquare size={13}/><span>{session.name||session.firstMessage||'未命名会话'}</span></button>)}</div>{sessions.length===0&&<p className="sidebar-empty">发送消息后自动保存</p>}</Disclosure>}
      <Disclosure title={<>后台驻守 <span>{tasks.length}</span></>} defaultOpen={tasks.length>0}><div className="watch-nav">{tasks.map(task=><button key={task.task_id} className={selectedId===task.task_id?'selected':''} title={task.label} onClick={()=>onWatch(task)}><span className={`nav-dot ${task.state}`}/><span>{task.label}</span>{task.state==='approval'&&<AlertCircle size={13}/>}</button>)}</div>{tasks.length===0&&<p className="sidebar-empty">在对话中描述等待条件</p>}</Disclosure>
    </div>
    <div className="sidebar-bottom">
      {workspace&&<div className="workspace-label" title={workspace}><FolderOpen size={17}/><span className="nav-label"><small>工作区</small>{workspace.split(/[\\/]/).filter(Boolean).at(-1)}</span></div>}
      <button className={`nav-item settings-nav ${view==='settings'?'selected':''}`} aria-label="设置" aria-current={view==='settings'?'page':undefined} onClick={()=>onView('settings')}><Settings size={18}/><span className="nav-label">设置</span>{tip('设置')}</button>
      <div className="sidebar-foot" role="status" title={error?'本地服务连接中断':connected?'本地服务已连接':'正在连接本地服务'}><span className={`connection-dot ${error?'offline':''}`}/><span className="nav-label">{error?'连接中断':connected?'本地服务已连接':'正在连接…'}</span><ChevronRight size={11} className="nav-label"/></div>
    </div>
  </aside>
}
