import {lazy,Suspense,useEffect,useRef,useState} from 'react'
import type React from 'react'
import {Globe,Monitor,TerminalSquare,X,ChevronUp,PictureInPicture2,PanelRight} from 'lucide-react'
import {gsap,useGSAP,useReducedMotion} from './Motion'
import {workspaceCommand,type PreviewRecord,type WorkspaceState,type WorkTab,type WorkTarget} from './task-workspace'
import type {ComputerJob,PlatformState} from './Platform'
import ComputerInspector from './ComputerInspector'
import {piControl} from './NativePi'
import './work-area.css'
const TerminalPane=lazy(()=>import('./TerminalPane'))
const PreviewPane=lazy(()=>import('./PreviewPane'))

type Place={x:number;y:number;w:number;h:number}
const clampPlace=(p:Place):Place=>{const w=Math.min(Math.max(p.w,320),innerWidth-16),h=Math.min(Math.max(p.h,260),innerHeight-16);return {w,h,x:Math.min(Math.max(p.x,8),innerWidth-w-8),y:Math.min(Math.max(p.y,8),innerHeight-h-8)}}
const savedPlace=():Place=>{try{const v=JSON.parse(localStorage.getItem('pulse:work-place')||'');if(typeof v?.x==='number')return clampPlace(v)}catch{/* first use */}return clampPlace({w:520,h:Math.min(620,innerHeight-120),x:innerWidth-548,y:72})}
const MIN_W=280, MAX_W_FRAC=0.65

/* WorkAreaResizer lives OUTSIDE the aside element in the chat-workspace flex row.
   It writes style.width directly on the asideRef — no React state, no GSAP interference. */
export function WorkAreaResizer({asideRef,floating,open}:{asideRef:React.RefObject<HTMLElement|null>;floating:boolean;open:boolean}){
  if(floating||!open)return null
  const onDown=(ev:React.PointerEvent<HTMLDivElement>)=>{
    const aside=asideRef.current
    if(ev.button!==0||!aside)return
    ev.preventDefault()
    const el=ev.currentTarget
    el.setPointerCapture(ev.pointerId)
    document.body.style.setProperty('cursor','col-resize','important')
    document.body.style.userSelect='none'
    const startX=ev.clientX,startW=aside.offsetWidth
    const move=(e:PointerEvent)=>{
      const next=Math.min(Math.max(startW-(e.clientX-startX),MIN_W),innerWidth*MAX_W_FRAC)
      aside.style.width=next+'px'
      aside.style.flexBasis=next+'px'
    }
    const up=()=>{
      document.body.style.removeProperty('cursor')
      document.body.style.userSelect=''
      el.removeEventListener('pointermove',move)
      localStorage.setItem('pulse:work-width',String(Math.round(aside.offsetWidth)))
    }
    el.addEventListener('pointermove',move)
    el.addEventListener('pointerup',up,{once:true})
    el.addEventListener('pointercancel',up,{once:true})
  }
  return <div className="work-area-resizer" aria-hidden="true" onPointerDown={onDown}/>
}

export function WorkArea({target,onTarget,platform,jobs,data,error,refresh,refreshPlatform,asideRef}:{target:WorkTarget|null;onTarget:(target:WorkTarget|null)=>void;platform:PlatformState;jobs:ComputerJob[];data:WorkspaceState|null;error:string;refresh:()=>void;refreshPlatform:()=>void;asideRef?:React.RefObject<HTMLElement|null>}){
  const [shown,setShown]=useState(!!target),[vertical,setVertical]=useState(()=>matchMedia('(max-width:1024px)').matches)
  const [narrow,setNarrow]=useState(()=>matchMedia('(max-width:720px)').matches)
  const [floatPref,setFloatPref]=useState(()=>localStorage.getItem('pulse:work-floating')==='1'),[place,setPlace]=useState(savedPlace)
  const floating=floatPref&&!narrow
  // Use the externally-supplied ref so App can position WorkAreaResizer alongside the aside.
  const internalRef=useRef<HTMLElement>(null)
  const root=(asideRef||internalRef) as React.RefObject<HTMLElement>
  const body=useRef<HTMLDivElement>(null),motion=useRef<ReturnType<typeof gsap.timeline>|null>(null)
  const previous=useRef<WorkTarget>({kind:'computer'})
  if(target)previous.current=target
  const active=target||previous.current,open=!!target,reduced=useReducedMotion()
  const savedDockedW=():number=>{const v=localStorage.getItem('pulse:work-width');return v?Math.max(MIN_W,Number(v)):420}
  useEffect(()=>{if(target)setShown(true)},[target])
  useEffect(()=>{const media=matchMedia('(max-width:1024px)'),small=matchMedia('(max-width:720px)');const update=()=>{setVertical(media.matches);setNarrow(small.matches)};media.addEventListener('change',update);small.addEventListener('change',update);return()=>{media.removeEventListener('change',update);small.removeEventListener('change',update)}},[])
  useEffect(()=>{localStorage.setItem('pulse:work-floating',floatPref?'1':'0');if(floatPref&&!localStorage.getItem('pulse:work-place'))setPlace(savedPlace())},[floatPref])
  useEffect(()=>{
    if(!floating||!root.current)return
    let timer:ReturnType<typeof setTimeout>
    const observer=new ResizeObserver(()=>{clearTimeout(timer);timer=setTimeout(()=>{const el=root.current;if(!el||!el.offsetWidth)return;const next=clampPlace({x:el.offsetLeft,y:el.offsetTop,w:el.offsetWidth,h:el.offsetHeight});localStorage.setItem('pulse:work-place',JSON.stringify(next));setPlace(next)},250)})
    observer.observe(root.current);return()=>{observer.disconnect();clearTimeout(timer)}
  },[floating])
  useEffect(()=>{const fit=()=>setPlace(p=>clampPlace(p));addEventListener('resize',fit);return()=>removeEventListener('resize',fit)},[])
  const drag=(event:React.PointerEvent<HTMLElement>)=>{
    if(!floating||event.button!==0||(event.target as HTMLElement).closest('button,select,input,a'))return
    const el=root.current;if(!el)return
    event.preventDefault()
    const startX=event.clientX-el.offsetLeft,startY=event.clientY-el.offsetTop,header=event.currentTarget
    header.setPointerCapture(event.pointerId);el.classList.add('dragging')
    const move=(e:PointerEvent)=>{const next=clampPlace({x:e.clientX-startX,y:e.clientY-startY,w:el.offsetWidth,h:el.offsetHeight});el.style.left=next.x+'px';el.style.top=next.y+'px'}
    const up=()=>{header.removeEventListener('pointermove',move);el.classList.remove('dragging');const next=clampPlace({x:el.offsetLeft,y:el.offsetTop,w:el.offsetWidth,h:el.offsetHeight});localStorage.setItem('pulse:work-place',JSON.stringify(next));setPlace(next)}
    header.addEventListener('pointermove',move);header.addEventListener('pointerup',up,{once:true});header.addEventListener('pointercancel',up,{once:true})
  }
  useGSAP(()=>{
    if(!root.current||!body.current)return
    motion.current?.kill()
    if(floating){
      gsap.set(body.current,{width:'100%',height:'100%',x:0,y:0})
      motion.current=gsap.timeline().fromTo(root.current,{opacity:open?0:1,scale:open?.96:1},{opacity:open?1:0,scale:open?1:.96,duration:reduced?0:.22,ease:'power3.out',onComplete:()=>{if(!open)setShown(false)}})
      return
    }
    gsap.set(root.current,{scale:1})
    const size=vertical?Math.min(340,innerHeight*.43):savedDockedW()
    gsap.set(root.current,vertical?{width:'100%'}:{height:'100%'})
    gsap.set(body.current,vertical?{width:'100%',height:size}:{width:size,height:'100%'})
    motion.current=gsap.timeline().to(root.current,{[vertical?'height':'width']:open?size:0,opacity:open?1:0,duration:reduced?0:open?.38:.34,ease:open?'power3.out':'power4.inOut',onComplete:()=>{if(!open)setShown(false)}},0).to(body.current,{[vertical?'y':'x']:open?0:20,opacity:open?1:0,duration:reduced?0:.24,overwrite:true},open&&!reduced?.06:0)
  },{scope:root,dependencies:[open,shown,vertical,reduced,floating]})
  const preview=async(url:string)=>{if(!data?.session_id)return;try{const item=await workspaceCommand<PreviewRecord>('preview_open',{url,session_id:data.session_id});refresh();onTarget({kind:'preview',id:item.id})}catch(e){setActionError((e as Error).message)}}
  const [actionError,setActionError]=useState('')
  if(!shown&&!target)return null
  const tabs:{id:WorkTab;name:string;Icon:typeof Monitor}[]=[{id:'computer',name:'电脑',Icon:Monitor},{id:'terminal',name:'终端',Icon:TerminalSquare},{id:'preview',name:'网页',Icon:Globe}]
  return <aside ref={root} className={`task-work-area ${floating?'floating':''}`} aria-label="任务工作区" inert={!open} style={floating?{left:place.x,top:place.y,width:place.w,height:place.h}:{[vertical?'height':'width']:0}}>
    <div ref={body} className="task-work-area-body"><header className="task-work-area-header" onPointerDown={drag} onDoubleClick={e=>{if(!narrow&&!(e.target as HTMLElement).closest('button'))setFloatPref(v=>!v)}} title={narrow?undefined:floating?'拖动移动 · 双击停靠':'双击浮动'}><div role="tablist" aria-label="任务工作区内容">{tabs.map(({id,name,Icon})=><button type="button" role="tab" key={id} aria-selected={active.kind===id} onClick={()=>{setActionError('');onTarget({kind:id})}}><Icon size={14}/><span>{name}</span>{id==='terminal'&&!!data?.terminals.filter(item=>item.state==='running').length&&<small>{data.terminals.filter(item=>item.state==='running').length}</small>}</button>)}</div><div className="task-work-area-tools">{!narrow&&<button type="button" className="icon-button" aria-label={floating?'停靠到右侧':'浮动窗口'} title={floating?'停靠到右侧':'浮动，可拖动和调整大小'} onClick={()=>setFloatPref(v=>!v)}>{floating?<PanelRight size={14}/>:<PictureInPicture2 size={14}/>}</button>}<button type="button" className="icon-button" aria-label="收起任务工作区" title="收起 · 进程继续运行" onClick={()=>onTarget(null)}>{vertical&&!floating?<ChevronUp size={15}/>:<X size={15}/>}</button></div></header>
    {(error||actionError)&&<div className="work-error" role="alert">{error||actionError}<button type="button" onClick={refresh}>重新读取</button></div>}
    <section className="task-work-area-content" aria-label={`${tabs.find(item=>item.id===active.kind)?.name}工作区`}><Suspense fallback={<div className="work-empty">正在打开…</div>}>
      {active.kind==='computer'?<ComputerInspector data={platform} target={active} jobs={jobs} refresh={refreshPlatform}/>:data&&!data.session_id?<div className="work-empty"><button type="button" className="secondary-button" onClick={()=>void piControl('start_agent').then(refresh).catch(e=>setActionError(e.message))}>启动 Pi 会话</button></div>:data?active.kind==='terminal'?<TerminalPane data={data} selected={active.id} onSelect={id=>onTarget({kind:'terminal',id})} refresh={refresh} onPreview={url=>void preview(url)}/>:<PreviewPane data={data} selected={active.id} onSelect={id=>onTarget({kind:'preview',id})} refresh={refresh}/>:<div className="work-empty">正在连接当前会话…</div>}
    </Suspense></section></div></aside>
}
