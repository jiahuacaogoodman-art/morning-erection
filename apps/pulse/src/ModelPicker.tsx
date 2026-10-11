import {useLayoutEffect, useRef, useState} from 'react'
import {createPortal} from 'react-dom'
import {ArrowUpRight, Brain, Check, ChevronDown, Cpu, Image, Search, SlidersHorizontal} from 'lucide-react'
import {piCommand, piControl} from './NativePi'
import {gsap, useGSAP, useReducedMotion} from './Motion'
import type {PiModel, PiSnapshot} from './types'
import {ThinkingControl, type ThinkingState} from './ThinkingControl'

type Model = PiModel & {provider_name?:string}

export function ModelPicker({pi,busy,refresh,thinking}:{pi:PiSnapshot;busy:boolean;refresh:()=>void;thinking:ThinkingState}) {
  const [open,setOpen]=useState<'model'|null>(null), [rendered,setRendered]=useState<typeof open>(null)
  const [models,setModels]=useState<Model[]>([]), [query,setQuery]=useState('')
  const [loading,setLoading]=useState(false), [changing,setChanging]=useState(false), [error,setError]=useState(''), [active,setActive]=useState(0)
  const [position,setPosition]=useState({left:0,bottom:0,width:296})
  const root=useRef<HTMLDivElement>(null), panel=useRef<HTMLDivElement>(null), modelButton=useRef<HTMLButtonElement>(null), search=useRef<HTMLInputElement>(null)
  const seenPanel=useRef<HTMLDivElement|null>(null), highlight=useRef<HTMLDivElement>(null), optionsRef=useRef<HTMLDivElement>(null)
  const panelAnimation=useRef<ReturnType<typeof gsap.timeline>|null>(null)
  const reduced=useReducedMotion(), current=pi.state?.model
  const filtered=models.filter(model=>`${model.id} ${model.name} ${model.provider_name||model.provider}`.toLowerCase().includes(query.toLowerCase()))
  const close=(restore=true)=>{const target=modelButton.current;setOpen(null);if(restore)requestAnimationFrame(()=>target?.focus())}
  const load=async()=>{
    setLoading(true);setError('');setModels([])
    try {
      {const available=await piControl<Model[]>('models');setModels(available.sort((a,b)=>Number(b.provider===current?.provider)-Number(a.provider===current?.provider)||a.provider.localeCompare(b.provider)||a.name.localeCompare(b.name)))}
    } catch(e){setError((e as Error).message)}finally{setLoading(false)}
  }
  const toggle=()=>{if(open){close();return}setQuery('');setActive(0);setOpen('model');setRendered('model');void load()}
  useLayoutEffect(()=>{
    if(!rendered)return
    const place=()=>{const rect=modelButton.current?.getBoundingClientRect();if(rect){const width=Math.min(296,innerWidth-24);setPosition({width,left:Math.max(12,Math.min(rect.left,innerWidth-width-12)),bottom:Math.max(12,innerHeight-rect.top+9)})}}
    place();window.addEventListener('resize',place)
    const outside=(event:PointerEvent)=>{if(!root.current?.contains(event.target as Node)&&!panel.current?.contains(event.target as Node))close(false)}
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!document.querySelector('[role="dialog"][aria-modal="true"]')){event.preventDefault();close()}}
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape)
    return()=>{window.removeEventListener('resize',place);document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape)}
  },[rendered,open,busy,current?.id])
  useGSAP(()=>{
    if(!panel.current)return
    panelAnimation.current?.kill()
    if(seenPanel.current!==panel.current){gsap.set(panel.current,{autoAlpha:0,y:12,scale:.965});seenPanel.current=panel.current}
    gsap.killTweensOf(panel.current)
    if(open){gsap.set(panel.current,{width:position.width,height:'auto',borderRadius:11});gsap.set(panel.current.children,{opacity:1});panelAnimation.current=gsap.timeline().to(panel.current,{autoAlpha:1,y:0,scale:1,duration:reduced?0:.32,ease:'back.out(1.08)',overwrite:true,onComplete:()=>search.current?.focus({preventScroll:true})})}
    else {const rect=modelButton.current?.getBoundingClientRect();panelAnimation.current=gsap.timeline().to(panel.current.children,{opacity:0,duration:reduced?0:.09},0).to(panel.current,{width:rect?.width||130,height:rect?.height||30,y:(rect?.height||30)+9,scale:1,borderRadius:8,duration:reduced?0:.34,ease:'power4.inOut',overwrite:true},0).to(panel.current,{autoAlpha:0,duration:reduced?0:.1,onComplete:()=>setRendered(null)},reduced?0:.24)}
  },{scope:panel,dependencies:[open,rendered,reduced]})
  useGSAP(()=>{
    const parent=optionsRef.current,selected=parent?.querySelector<HTMLElement>('#model-option-'+active)
    if(!parent||!highlight.current||!selected)return
    const rect=selected.getBoundingClientRect(),container=parent.getBoundingClientRect()
    gsap.to(highlight.current,{y:rect.top-container.top+parent.scrollTop,height:rect.height,opacity:1,duration:reduced?0:.22,ease:'power3.out',overwrite:true})
  },{scope:optionsRef,dependencies:[active,query,models,rendered,reduced]})
  const chooseModel=async(model:Model)=>{
    if(busy||changing)return
    setChanging(true);setError('')
    try {await piCommand('set_model',{provider:model.provider,modelId:model.id});refresh();close()}
    catch(e){setError((e as Error).message)}finally{setChanging(false)}
  }
  const configure=()=>{close(false);location.hash='settings/connections'}
  return <div ref={root} className="model-picker">
    <button ref={modelButton} type="button" className={`model-chip ${open==='model'?'is-open':''}`} aria-label={`选择模型，当前 ${current?.name||'尚未选择'}`} aria-haspopup="dialog" aria-expanded={open==='model'} onClick={toggle}>
      <span className="model-chip-symbol"><Cpu size={13}/></span><span>{current?.name||'选择模型'}</span><ChevronDown size={11}/>
    </button>
    <ThinkingControl state={thinking} onOpen={()=>close(false)}/>
    {rendered&&createPortal(<div ref={panel} className="model-popover" role="dialog" aria-label="选择模型" aria-modal="false" inert={!open} style={{position:'fixed',...position,opacity:0,visibility:'hidden',transformOrigin:'bottom left'}}>
      <header className="model-popover-heading"><span>选择模型</span><small>当前会话</small></header>
      {busy&&<p className="model-picker-notice">当前任务完成或停止后，可以切换。</p>}
      {error&&<div className="model-picker-error" role="alert">{error}<button type="button" onClick={()=>void load()}>重新读取</button></div>}
      {<>
        <div className="model-search"><Search size={14}/><input ref={search} type="search" role="combobox" aria-expanded={true} aria-controls="available-models" aria-activedescendant={filtered[active]?'model-option-'+active:undefined} aria-autocomplete="list" aria-label="搜索模型" placeholder="搜索模型或提供商…" value={query} onChange={e=>{setQuery(e.target.value);setActive(0)}} onKeyDown={e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();const next=(active+(e.key==='ArrowDown'?1:-1)+Math.max(1,filtered.length))%Math.max(1,filtered.length);setActive(next);document.getElementById('model-option-'+next)?.scrollIntoView({block:'nearest'})}if(e.key==='Enter'&&filtered[active]){e.preventDefault();void chooseModel(filtered[active])}}}/></div>
        <div ref={optionsRef} className="model-options" role="listbox" id="available-models" aria-label="可用模型"><div ref={highlight} className="model-gliding-highlight" aria-hidden="true"/>{filtered.map((model,index)=>{
          const selected=current?.provider===model.provider&&current?.id===model.id
          const provider=model.provider_name||(model.provider.startsWith('gateway-')?'模型网关':model.provider)
          const previous=filtered[index-1];const heading=!previous||previous.provider!==model.provider
          return <div key={model.provider+'::'+model.id}>{heading&&<div className="model-provider-label">{provider}</div>}<button id={'model-option-'+index} type="button" role="option" aria-selected={selected} disabled={busy||changing||loading} className={`model-option ${selected?'selected':''} ${active===index?'keyboard-active':''}`} onPointerMove={()=>setActive(index)} onClick={()=>void chooseModel(model)}>
            <span className="model-option-symbol"><Cpu size={15}/></span><span className="model-option-copy"><strong>{model.name}</strong><span>{model.reasoning&&<span><Brain size={10}/>思考</span>}{model.input.includes('image')&&<span><Image size={10}/>图片</span>}<span>{model.contextWindow>=1000000?`${Number((model.contextWindow/1000000).toFixed(2))}M`:`${Math.round(model.contextWindow/1000)}k`} 上下文</span></span></span>{selected?<Check size={15} className="model-selected"/>:<span className="model-choice-dot"/>}
          </button></div>
        })}</div>
        {loading&&<p className="model-picker-notice" role="status">正在读取 Pi 模型…</p>}{!loading&&!filtered.length&&!error&&<div className="model-picker-empty">{models.length?'没有匹配的模型':'尚未连接模型服务'}</div>}
        <button type="button" className="model-manage" onClick={configure}><SlidersHorizontal size={13}/>管理模型与连接<ArrowUpRight size={12}/></button>
      </>}
    </div>,document.body)}
  </div>
}
