import {useEffect,useRef,useState} from 'react'
import {Terminal} from '@xterm/xterm'
import {FitAddon} from '@xterm/addon-fit'
import {WebLinksAddon} from '@xterm/addon-web-links'
import {ArrowUpRight,Plus,Square,Trash2,RefreshCw} from 'lucide-react'
import {workspaceCommand,type TerminalRecord,type WorkspaceState} from './task-workspace'
import '@xterm/xterm/css/xterm.css'

type ReadResult={terminal:TerminalRecord;output:string;cursor:number;reset:boolean;urls:string[]}
export default function TerminalPane({data,selected,onSelect,refresh,onPreview}:{data:WorkspaceState;selected?:string;onSelect:(id:string)=>void;refresh:()=>void;onPreview:(url:string)=>void}){
  const [busy,setBusy]=useState(''),[error,setError]=useState('')
  const active=data.terminals.find(item=>item.id===selected)||data.terminals[0]
  const run=async(op:string,id?:string)=>{
    setBusy(op);setError('')
    try{const result=await workspaceCommand<TerminalRecord>(op,{id,session_id:data.session_id,request_id:crypto.randomUUID(),title:`终端 ${data.terminals.length+1}`});refresh();if(op==='terminal_create')onSelect(result.id)}
    catch(e){setError((e as Error).message)}finally{setBusy('')}
  }
  return <div className="task-terminal-pane">
    <div className="work-resource-toolbar"><div role="tablist" aria-label="任务终端">{data.terminals.map((item,index)=><button type="button" role="tab" key={item.id} aria-selected={item.id===active?.id} onClick={()=>onSelect(item.id)} title={item.title}><i className={`resource-dot ${item.state}`}/>{item.title||`终端 ${index+1}`}</button>)}</div><button type="button" className="icon-button" aria-label="新建终端" title="新建终端 · 与对话并行" disabled={!!busy||!data.session_id||!data.available} onClick={()=>void run('terminal_create')}><Plus size={14}/></button></div>
    {error&&<p className="work-error" role="alert">{error}</p>}
    {active?<><div className="terminal-session-meta"><span title={active.workspace}>{active.workspace.split('/').at(-1)}</span><span>{active.state==='running'?'运行中':active.state==='completed'?`已退出 · ${active.exit_code}`:active.state==='cancelled'?'已停止':active.state==='interrupted'?'服务重启后已中断':'退出异常'}</span><button type="button" className="icon-button" aria-label={active.state==='running'?'停止这个终端':'移除终端记录'} title={active.state==='running'?'停止这个终端':'移除终端记录'} disabled={!!busy} onClick={()=>void run(active.state==='running'?'terminal_stop':'terminal_remove',active.id)}>{active.state==='running'?<Square size={12}/>:<Trash2 size={13}/>}</button></div><TerminalSurface key={active.id} item={active} sessionId={data.session_id!} onPreview={onPreview} refresh={refresh}/></>:<div className="work-empty"><button type="button" className="secondary-button" disabled={!!busy||!data.session_id||!data.available} onClick={()=>void run('terminal_create')}><Plus size={14}/>打开终端</button></div>}
  </div>
}

function TerminalSurface({item,sessionId,onPreview,refresh}:{item:TerminalRecord;sessionId:string;onPreview:(url:string)=>void;refresh:()=>void}){
  const element=useRef<HTMLDivElement>(null),[error,setError]=useState(''),[urls,setUrls]=useState<string[]>([]),[retry,setRetry]=useState(0)
  const preview=useRef(onPreview),refreshRef=useRef(refresh);preview.current=onPreview;refreshRef.current=refresh
  useEffect(()=>{
    if(!element.current)return
    let stopped=false,cursor=0,timer:ReturnType<typeof setTimeout>,resizeTimer:ReturnType<typeof setTimeout>,inputQueue=Promise.resolve(),lastState=item.state
    const controller=new AbortController(),fit=new FitAddon()
    const colors=getComputedStyle(document.documentElement)
    const term=new Terminal({fontSize:12,fontFamily:'ui-monospace, SFMono-Regular, Menlo, monospace',cursorBlink:!matchMedia('(prefers-reduced-motion: reduce)').matches,scrollback:3000,screenReaderMode:true,allowProposedApi:false,
      theme:{background:colors.getPropertyValue('--surface').trim(),foreground:colors.getPropertyValue('--ink').trim(),cursor:colors.getPropertyValue('--accent').trim(),selectionBackground:'#bd704533'}})
    term.loadAddon(fit);term.loadAddon(new WebLinksAddon((event,url)=>{event.preventDefault();if(/^https?:\/\//i.test(url))preview.current(url)}))
    term.open(element.current);term.textarea?.setAttribute('aria-label','终端输入');term.options.disableStdin=item.state!=='running'
    const input=term.onData(data=>{const input_id=crypto.randomUUID();inputQueue=inputQueue.then(async()=>{if(stopped)return;await workspaceCommand('terminal_input',{id:item.id,session_id:sessionId,data,input_id})}).catch(e=>{if(!stopped)setError((e as Error).message)})})
    const resize=()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(stopped||!element.current?.clientWidth)return;fit.fit();if(lastState==='running')void workspaceCommand('terminal_resize',{id:item.id,session_id:sessionId,cols:Math.max(20,Math.min(300,term.cols)),rows:Math.max(4,Math.min(100,term.rows))}).catch(()=>{})},120)}
    const observer=new ResizeObserver(resize);observer.observe(element.current);resize()
    const poll=async()=>{
      try{const result=await workspaceCommand<ReadResult>('terminal_read',{id:item.id,session_id:sessionId,cursor},controller.signal);if(stopped)return
        if(result.reset)term.reset();if(result.output)term.write(result.output);cursor=result.cursor;setUrls(result.urls);setError('');term.options.disableStdin=result.terminal.state!=='running'
        if(result.terminal.state!==lastState){lastState=result.terminal.state;refreshRef.current()}
      }catch(e){if(!stopped)setError((e as Error).message)}finally{if(!stopped)timer=setTimeout(poll,lastState==='running'?80:1500)}
    }
    void poll();return()=>{stopped=true;controller.abort();clearTimeout(timer);clearTimeout(resizeTimer);observer.disconnect();input.dispose();term.dispose()}
  },[item.id,sessionId,retry])
  return <><div className="task-terminal-screen" ref={element} aria-label="终端输出"/>{error&&<div className="work-error" role="alert">{error}<button type="button" onClick={()=>setRetry(value=>value+1)}><RefreshCw size={12}/>重新连接</button></div>}{!!urls.length&&<div className="terminal-links" aria-label="终端发现的网址">{urls.map(url=><button type="button" key={url} title={url} onClick={()=>onPreview(url)}><ArrowUpRight size={12}/>{url}</button>)}</div>}</>
}
