import {lazy,Suspense,useEffect,useRef,useState} from 'react'
import {Globe,Monitor,TerminalSquare,X,ChevronUp} from 'lucide-react'
import {gsap,useGSAP,useReducedMotion} from './Motion'
import {workspaceCommand,type PreviewRecord,type WorkspaceState,type WorkTab,type WorkTarget} from './task-workspace'
import type {ComputerJob,PlatformState} from './Platform'
import ComputerInspector from './ComputerInspector'
import {piControl} from './NativePi'
import './work-area.css'
const TerminalPane=lazy(()=>import('./TerminalPane'))
const PreviewPane=lazy(()=>import('./PreviewPane'))

export function WorkArea({target,onTarget,platform,jobs,data,error,refresh,refreshPlatform}:{target:WorkTarget|null;onTarget:(target:WorkTarget|null)=>void;platform:PlatformState;jobs:ComputerJob[];data:WorkspaceState|null;error:string;refresh:()=>void;refreshPlatform:()=>void}){
  const [shown,setShown]=useState(!!target),[vertical,setVertical]=useState(()=>matchMedia('(max-width:1024px)').matches)
  const previous=useRef<WorkTarget>({kind:'computer'}),root=useRef<HTMLElement>(null),body=useRef<HTMLDivElement>(null),motion=useRef<ReturnType<typeof gsap.timeline>|null>(null)
  if(target)previous.current=target
  const active=target||previous.current,open=!!target,reduced=useReducedMotion()
  useEffect(()=>{if(target)setShown(true)},[target])
  useEffect(()=>{const media=matchMedia('(max-width:1024px)');const update=()=>setVertical(media.matches);media.addEventListener('change',update);return()=>media.removeEventListener('change',update)},[])
  useGSAP(()=>{
    if(!root.current||!body.current)return
    motion.current?.kill();const size=vertical?Math.min(340,innerHeight*.43):Math.min(460,innerWidth*.37)
    gsap.set(root.current,vertical?{width:'100%'}:{height:'100%'})
    gsap.set(body.current,vertical?{width:'100%',height:size}:{width:size,height:'100%'})
    motion.current=gsap.timeline().to(root.current,{[vertical?'height':'width']:open?size:0,opacity:open?1:0,duration:reduced?0:open?.38:.34,ease:open?'power3.out':'power4.inOut',onComplete:()=>{if(!open)setShown(false)}},0).to(body.current,{[vertical?'y':'x']:open?0:20,opacity:open?1:0,duration:reduced?0:.24,overwrite:true},open&&!reduced?.06:0)
  },{scope:root,dependencies:[open,shown,vertical,reduced]})
  const preview=async(url:string)=>{if(!data?.session_id)return;try{const item=await workspaceCommand<PreviewRecord>('preview_open',{url,session_id:data.session_id});refresh();onTarget({kind:'preview',id:item.id})}catch(e){setActionError((e as Error).message)}}
  const [actionError,setActionError]=useState('')
  if(!shown&&!target)return null
  const tabs:{id:WorkTab;name:string;Icon:typeof Monitor}[]=[{id:'computer',name:'电脑',Icon:Monitor},{id:'terminal',name:'终端',Icon:TerminalSquare},{id:'preview',name:'网页',Icon:Globe}]
  return <aside ref={root} className="task-work-area" aria-label="任务工作区" inert={!open} style={{[vertical?'height':'width']:0}}><div ref={body} className="task-work-area-body"><header className="task-work-area-header"><div role="tablist" aria-label="任务工作区内容">{tabs.map(({id,name,Icon})=><button type="button" role="tab" key={id} aria-selected={active.kind===id} onClick={()=>{setActionError('');onTarget({kind:id})}}><Icon size={14}/><span>{name}</span>{id==='terminal'&&!!data?.terminals.filter(item=>item.state==='running').length&&<small>{data.terminals.filter(item=>item.state==='running').length}</small>}</button>)}</div><button type="button" className="icon-button" aria-label="收起任务工作区" title="收起 · 进程继续运行" onClick={()=>onTarget(null)}>{vertical?<ChevronUp size={15}/>:<X size={15}/>}</button></header>
    {(error||actionError)&&<div className="work-error" role="alert">{error||actionError}<button type="button" onClick={refresh}>重新读取</button></div>}
    <section className="task-work-area-content" aria-label={`${tabs.find(item=>item.id===active.kind)?.name}工作区`}><Suspense fallback={<div className="work-empty">正在打开…</div>}>
      {active.kind==='computer'?<ComputerInspector data={platform} target={active} jobs={jobs} refresh={refreshPlatform}/>:data&&!data.session_id?<div className="work-empty"><button type="button" className="secondary-button" onClick={()=>void piControl('start_agent').then(refresh).catch(e=>setActionError(e.message))}>启动 Pi 会话</button></div>:data?active.kind==='terminal'?<TerminalPane data={data} selected={active.id} onSelect={id=>onTarget({kind:'terminal',id})} refresh={refresh} onPreview={url=>void preview(url)}/>:<PreviewPane data={data} selected={active.id} onSelect={id=>onTarget({kind:'preview',id})} refresh={refresh}/>:<div className="work-empty">正在连接当前会话…</div>}
    </Suspense></section></div></aside>
}
