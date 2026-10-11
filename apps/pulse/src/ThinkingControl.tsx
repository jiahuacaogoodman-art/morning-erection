import {useCallback, useEffect, useRef, useState} from 'react'
import type {KeyboardEvent, PointerEvent} from 'react'
import {Brain, LoaderCircle} from 'lucide-react'
import {piCommand} from './NativePi'
import {gsap, useGSAP, useReducedMotion} from './Motion'
import type {PiSnapshot} from './types'

export const thinkingNames:Record<string,string>={off:'关闭',minimal:'极简',low:'轻量',medium:'均衡',high:'深入',xhigh:'更深入',max:'最深'}
export const thinkingDescriptions:Record<string,string>={off:'不使用独立思考预算',minimal:'最低推理强度',low:'较低推理强度',medium:'中等推理强度',high:'较高推理强度',xhigh:'极高推理强度',max:'模型支持的最高推理强度'}

// One native state for drag, keyboard and slash commands. No local-only setting.
export function useThinkingLevel(pi:PiSnapshot|null|undefined,busy:boolean,refresh:()=>void) {
  const key=`${pi?.state?.sessionId}:${pi?.state?.model?.provider}:${pi?.state?.model?.id}`
  const currentKey=useRef(key);currentKey.current=key
  const [levels,setLevels]=useState<string[]>([]),[loading,setLoading]=useState(false),[changing,setChanging]=useState(false),[error,setError]=useState('')
  const [value,setValue]=useState(pi?.state?.thinkingLevel||'off')
  const locked=useRef(false),alive=useRef(true),readVersion=useRef(0)
  useEffect(()=>{alive.current=true;return()=>{alive.current=false}},[])
  useEffect(()=>{setValue(pi?.state?.thinkingLevel||'off');setError('')},[key,pi?.state?.thinkingLevel])
  const load=useCallback(async()=>{
    const version=++readVersion.current
    if(!pi?.agent_ready||!pi.state?.model?.reasoning){setLevels([]);setLoading(false);return}
    setLoading(true);setError('')
    try{const result=await piCommand<{levels:string[]}>('get_available_thinking_levels');if(alive.current&&currentKey.current===key&&version===readVersion.current)setLevels(result.levels)}
    catch(e){if(alive.current&&currentKey.current===key&&version===readVersion.current)setError((e as Error).message)}
    finally{if(alive.current&&version===readVersion.current)setLoading(false)}
  },[key,pi?.agent_ready,pi?.state?.model?.reasoning])
  useEffect(()=>{setLevels([]);void load();return()=>{readVersion.current++}},[load])
  const choose=async(next:string)=>{
    if(busy||locked.current||loading||!levels.includes(next))return false
    if(next===value){setError('');return true}
    locked.current=true;setChanging(true);setError('')
    try{
      await piCommand('set_thinking_level',{level:next})
      const state=await piCommand<{thinkingLevel:string}>('get_state')
      if(alive.current&&key===currentKey.current){setValue(state.thinkingLevel);refresh()}
      return key===currentKey.current
    }catch(e){if(alive.current&&key===currentKey.current)setError((e as Error).message);return false}
    finally{locked.current=false;if(alive.current)setChanging(false)}
  }
  return {key,value,levels,loading,changing,error,load,choose,busy,supported:!!pi?.state?.model?.reasoning}
}
export type ThinkingState=ReturnType<typeof useThinkingLevel>

export function ThinkingControl({state,onOpen}:{state:ThinkingState;onOpen?:()=>void}) {
  const {levels,value,loading,changing,busy,error}=state
  const index=Math.max(0,levels.indexOf(value)),disabled=busy||changing||loading||!levels.length
  const [preview,setPreview]=useState(index),[dragging,setDragging]=useState(false)
  const root=useRef<HTMLDivElement>(null),control=useRef<HTMLDivElement>(null),light=useRef<HTMLSpanElement>(null)
  const drag=useRef<{id:number;x:number;index:number;step:number;width:number;maxWidth:number;moved:boolean}|null>(null),previewRef=useRef(index)
  const reduced=useReducedMotion(),{contextSafe}=useGSAP({scope:root})
  useEffect(()=>{if(!drag.current){setPreview(index);previewRef.current=index}},[index,value,state.key])
  const settle=contextSafe(()=>{
    gsap.to(control.current,{width:root.current?.clientWidth||88,scaleY:1,duration:reduced?0:.64,ease:'elastic.out(1, .5)',overwrite:true})
    gsap.to(light.current,{opacity:.45,duration:reduced?0:.35,overwrite:true})
  })
  const cancel=()=>{
    const active=drag.current;drag.current=null;setDragging(false);setPreview(index);previewRef.current=index
    if(active&&control.current?.hasPointerCapture(active.id))control.current.releasePointerCapture(active.id)
    settle()
  }
  useEffect(()=>{cancel()},[state.key])
  useEffect(()=>{if(disabled&&drag.current)cancel()},[disabled])
  useEffect(()=>{const resize=()=>cancel();window.addEventListener('resize',resize);return()=>window.removeEventListener('resize',resize)},[index])
  const commit=(next:number)=>{
    setPreview(next);previewRef.current=next
    void state.choose(levels[next]).then(ok=>{if(!ok){setPreview(index);previewRef.current=index}})
  }
  const start=(event:PointerEvent<HTMLDivElement>)=>{
    if(disabled||event.button!==0||drag.current)return
    event.preventDefault();onOpen?.();event.currentTarget.focus({preventScroll:true});event.currentTarget.setPointerCapture(event.pointerId)
    const rect=event.currentTarget.getBoundingClientRect(),composer=event.currentTarget.closest('.composer')?.getBoundingClientRect()
    drag.current={id:event.pointerId,x:event.clientX,index,step:26,width:rect.width,maxWidth:Math.max(rect.width,Math.min(220,(composer?.right||innerWidth)-rect.left-12)),moved:false}
    setDragging(true);previewRef.current=index;setPreview(index)
    gsap.killTweensOf(control.current)
  }
  const move=contextSafe((event:PointerEvent<HTMLDivElement>)=>{
    const d=drag.current;if(!d||d.id!==event.pointerId)return
    const dx=event.clientX-d.x;if(Math.abs(dx)>4)d.moved=true
    const raw=d.index+dx/d.step,clamped=Math.max(0,Math.min(levels.length-1,raw)),next=Math.round(clamped)
    const wanted=d.width+dx*.72,bounded=Math.max(72,Math.min(d.maxWidth,wanted)),over=wanted-bounded
    const width=bounded+Math.sign(over)*Math.min(9,Math.sqrt(Math.abs(over)))
    gsap.set(control.current,{width,scaleY:1-Math.min(Math.abs(dx)/2500,.055)})
    gsap.set(light.current,{opacity:.85})
    setPreview(next);previewRef.current=next
  })
  const finish=(event:PointerEvent<HTMLDivElement>)=>{
    const d=drag.current;if(!d||d.id!==event.pointerId)return
    drag.current=null;setDragging(false);if(control.current?.hasPointerCapture(d.id))control.current.releasePointerCapture(d.id)
    settle();commit(d.moved?previewRef.current:(index+1)%levels.length)
  }
  const keyboard=(event:KeyboardEvent<HTMLDivElement>)=>{
    if(event.key==='Escape'&&drag.current){event.preventDefault();cancel();return}
    if(disabled)return
    let next=index
    if(event.key==='ArrowRight'||event.key==='ArrowUp')next++
    else if(event.key==='ArrowLeft'||event.key==='ArrowDown')next--
    else if(event.key==='Home')next=0
    else if(event.key==='End')next=levels.length-1
    else if(event.key===' '||event.key==='Enter')next=(index+1)%levels.length
    else return
    event.preventDefault();onOpen?.();commit(Math.max(0,Math.min(levels.length-1,next)));settle()
  }
  if(!state.supported)return null
  return <div ref={root} className="thinking-elastic-wrap">
    <div ref={control} className={`thinking-elastic ${dragging?'is-dragging':''}`} role="slider" tabIndex={0} aria-label="思考深度" aria-valuemin={0} aria-valuemax={Math.max(0,levels.length-1)} aria-valuenow={preview} aria-valuetext={thinkingNames[levels[preview]]||thinkingNames[value]||value} aria-disabled={disabled} aria-orientation="horizontal" title={busy?'任务完成后可调整':`思考深度：${thinkingNames[value]||value} · 左右拖动调整，点按切换，也可输入 /`} onPointerDown={start} onPointerMove={move} onPointerUp={finish} onPointerCancel={cancel} onLostPointerCapture={()=>{if(drag.current)cancel()}} onKeyDown={keyboard}>
      <span ref={light} className="thinking-elastic-light" aria-hidden="true" style={{background:`linear-gradient(90deg, var(--accent-wash) ${preview/Math.max(1,levels.length-1)*100}%, transparent)`}}/>
      {changing?<LoaderCircle size={12} className="thinking-saving"/>:<Brain size={12}/>}
      <span className="thinking-elastic-label">{thinkingNames[levels[preview]]||thinkingNames[value]||value}</span>
      <span className="thinking-elastic-grip" aria-hidden="true">{[0,1,2].map(i=><i key={i}/>)}</span>
    </div>
    {error&&<span className="thinking-inline-error" role="alert">{error}<button type="button" onClick={()=>void state.load()}>重试</button></span>}
  </div>
}
