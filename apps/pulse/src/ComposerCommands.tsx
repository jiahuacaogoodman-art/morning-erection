import {useEffect, useRef, useState} from 'react'
import type {KeyboardEvent, RefObject} from 'react'
import {ArrowLeft, ArrowUpRight, Brain, Check, ChevronRight, Command, LoaderCircle} from 'lucide-react'
import {piCommand} from './NativePi'
import {thinkingNames, thinkingDescriptions, type ThinkingState} from './ThinkingControl'
import {gsap,useGSAP,useReducedMotion} from './Motion'
import type {PiSnapshot} from './types'

type NativeCommand={name:string;description?:string;source:string}
type Item={id:string;title:string;description:string;kind:'thinking'|'level'|'native';value?:string}

export function useComposerCommands({pi,thinking,text,setText,input}:{pi:PiSnapshot|null|undefined;thinking:ThinkingState;text:string;setText:(value:string)=>void;input:RefObject<HTMLTextAreaElement|null>}) {
  const [caret,setCaret]=useState(text.length),[dismissed,setDismissed]=useState(''),[active,setActive]=useState(0)
  const [commands,setCommands]=useState<NativeCommand[]>([]),[loading,setLoading]=useState(false),[error,setError]=useState(''),[reload,setReload]=useState(0)
  const panel=useRef<HTMLDivElement>(null),reduced=useReducedMotion(),textRef=useRef(text);textRef.current=text
  const prefix=text.slice(0,caret),match=prefix.match(/(?:^|\s)\/([^\s/]*)(?:[ \t]+([^\s/]*))?$/)
  const start=match?prefix.length-match[0].length+(match[0].startsWith('/')?0:1):-1
  const query=match?.[1]||'',arg=match?.[2],tokenKey=`${start}:${caret}:${prefix}`
  const candidate=!!pi?.agent_ready&&start>=0&&dismissed!==tokenKey
  // Keep extension names authoritative. The GUI action picks a free name.
  let localName='think';while(commands.some(item=>item.name===localName))localName='morning:'+localName
  const isLocal=query===localName||query==='思考'&&!commands.some(item=>item.name==='思考')
  const levelMode=isLocal&&arg!==undefined
  const visible=candidate&&(arg===undefined||levelMode)
  useEffect(()=>{if(!candidate)return;let alive=true;setLoading(true);setError('')
    void piCommand<{commands:NativeCommand[]}>('get_commands').then(result=>{if(alive)setCommands(result.commands)}).catch(e=>{if(alive)setError(e.message)}).finally(()=>{if(alive)setLoading(false)})
    return()=>{alive=false}
  },[candidate,pi?.state?.sessionId,reload])
  useEffect(()=>{setActive(0)},[query,arg,levelMode])
  useEffect(()=>{if(!visible)return;const outside=(event:PointerEvent)=>{
    // Keep the picker alive through the form's submit event. Otherwise pointerdown
    // dismisses it before onSubmit can consume the selection as a local action.
    const submit=event.target instanceof Element?event.target.closest<HTMLButtonElement>('.send-button'):null
    if(!panel.current?.contains(event.target as Node)&&event.target!==input.current&&submit?.form!==input.current?.form)setDismissed(tokenKey)
  };document.addEventListener('pointerdown',outside);return()=>document.removeEventListener('pointerdown',outside)},[visible,tokenKey])
  const items:Item[]=levelMode?thinking.levels.filter(value=>`${value} ${thinkingNames[value]}`.toLowerCase().includes((arg||'').toLowerCase())).map(value=>({id:value,kind:'level',value,title:thinkingNames[value]||value,description:thinkingDescriptions[value]||value})):[
    ...(thinking.supported&&!commands.some(item=>item.name===query)&&(!query||`${localName} 思考 深度 thinking effort`.includes(query.toLowerCase()))?[{id:'local-thinking',kind:'thinking' as const,title:'思考深度',description:'拖动或选择当前会话的思考档位'}]:[]),
    ...commands.filter(item=>`${item.name} ${item.description||''}`.toLowerCase().includes(query.toLowerCase())).map(item=>({id:`native:${item.name}`,kind:'native' as const,value:item.name,title:`/${item.name}`,description:item.description||({skill:'Skill',extension:'扩展命令',prompt:'提示词'}[item.source]||item.source)}))
  ]
  const selected=Math.min(active,Math.max(0,items.length-1))
  useGSAP(()=>{if(visible&&panel.current)gsap.fromTo(panel.current,{autoAlpha:0,y:9,scale:.985},{autoAlpha:1,y:0,scale:1,duration:reduced?0:.26,ease:'back.out(1.05)'})},{scope:panel,dependencies:[visible,levelMode,reduced]})
  const replace=(replacement:string)=>{
    const next=text.slice(0,start)+replacement+text.slice(caret)
    setText(next);const nextCaret=start+replacement.length;setCaret(nextCaret)
    requestAnimationFrame(()=>{input.current?.focus({preventScroll:true});input.current?.setSelectionRange(nextCaret,nextCaret)})
  }
  const choose=async(item:Item)=>{
    if(loading||error)return
    if(item.kind==='thinking'){replace(`/${localName} `);return}
    if(item.kind==='native'){replace(`/${item.value} `);return}
    if(thinking.busy||thinking.changing)return
    const original=text
    if(await thinking.choose(item.value!)){
      // A slow response must never overwrite text the user has since edited.
      if(textRef.current===original)replace('')
      else setDismissed(tokenKey)
    }
  }
  const keyDown=(event:KeyboardEvent<HTMLTextAreaElement>)=>{
    if(!visible||event.nativeEvent.isComposing)return false
    if(event.key==='Escape'){event.preventDefault();setDismissed(tokenKey);return true}
    if(event.key==='ArrowDown'||event.key==='ArrowUp'||levelMode&&(event.key==='ArrowLeft'||event.key==='ArrowRight')){
      event.preventDefault();const next=(selected+(['ArrowDown','ArrowRight'].includes(event.key)?1:-1)+Math.max(1,items.length))%Math.max(1,items.length);setActive(next)
      document.getElementById('composer-command-'+next)?.scrollIntoView({block:'nearest'});return true
    }
    if((event.key==='Enter'&&!event.shiftKey)||event.key==='Tab'&&!event.shiftKey&&items.length){
      // Unknown native commands still go to Pi; incomplete local settings never do.
      if(items.length||levelMode||loading){event.preventDefault();if(items[selected])void choose(items[selected]);return true}
    }
    return false
  }
  const interceptSubmit=()=>{if(visible&&(items.length||levelMode||loading)){if(items[selected])void choose(items[selected]);return true}return false}
  const trackCaret=()=>setCaret(input.current?.selectionStart??text.length)
  return {
    keyDown,interceptSubmit,trackCaret,
    inputProps:{role:'combobox' as const,'aria-autocomplete':'list' as const,'aria-haspopup':'listbox' as const,'aria-expanded':visible,'aria-controls':visible?'composer-command-list':undefined,'aria-activedescendant':visible&&items[selected]?'composer-command-'+selected:undefined},
    menu:visible?<div ref={panel} className={`composer-command-menu ${levelMode?'is-thinking':''}`}>
      <header>{levelMode?<button type="button" onClick={()=>replace('/')} aria-label="返回快捷选择"><ArrowLeft size={13}/></button>:<Command size={13}/>}<strong>{levelMode?'思考深度':'快捷选择'}</strong><span>{levelMode?`当前 · ${thinkingNames[thinking.value]||thinking.value}`:'Pi 原生命令'}</span><kbd>esc</kbd></header>
      {thinking.busy&&levelMode&&<p className="command-notice">任务完成或停止后可调整思考深度</p>}
      <div id="composer-command-list" role="listbox" aria-orientation={levelMode?'horizontal':'vertical'} aria-label={levelMode?'选择思考深度':'对话快捷选择'} className="composer-command-options">{items.map((item,i)=><button type="button" id={'composer-command-'+i} key={item.id} role="option" aria-label={`${item.title} ${item.description}`} title={item.description} data-current={item.kind==='level'&&item.value===thinking.value} aria-selected={i===selected} aria-disabled={loading||item.kind==='level'&&(thinking.busy||thinking.changing)} className={i===selected?'active':''} onPointerMove={()=>setActive(i)} onMouseDown={e=>e.preventDefault()} onClick={()=>void choose(item)}>
        <span className={`command-icon ${item.kind==='native'?'native':''}`}>{item.kind==='native'?<Command size={15}/>:<Brain size={15}/>}</span><span className="command-copy"><strong>{item.title}</strong><small>{item.description}</small></span>{item.kind==='thinking'?<ChevronRight size={13}/>:item.kind==='level'?item.value===thinking.value&&<Check size={14}/>:<ArrowUpRight size={12}/>}
      </button>)}</div>
      {(loading||thinking.loading&&levelMode)&&<p className="command-notice"><LoaderCircle size={12}/>正在读取 Pi 选项…</p>}
      {!loading&&!items.length&&<p className="command-notice">{levelMode?'没有匹配的思考档位':'没有匹配的命令，可继续输入并发送给 Pi。'}</p>}
      {(error||levelMode&&thinking.error)&&<div className="thinking-error" role="alert">{error||thinking.error}{error&&<button type="button" onClick={()=>setReload(value=>value+1)}>重新读取</button>}</div>}
      <footer><span><kbd>{levelMode?'←':'↑'}</kbd><kbd>{levelMode?'→':'↓'}</kbd>选择</span><span><kbd>↵</kbd>{levelMode?'应用档位':'选择'}</span></footer>
    </div>:null
  }
}
