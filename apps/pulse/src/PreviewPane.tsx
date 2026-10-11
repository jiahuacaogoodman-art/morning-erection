import {useEffect,useState} from 'react'
import {ArrowUpRight,Plus,RefreshCw,X} from 'lucide-react'
import {workspaceCommand,type PreviewRecord,type WorkspaceState} from './task-workspace'

export default function PreviewPane({data,selected,onSelect,refresh}:{data:WorkspaceState;selected?:string;onSelect:(id:string)=>void;refresh:()=>void}){
  const active=data.previews.find(item=>item.id===selected)||data.previews[0]
  const [address,setAddress]=useState(active?.url||''),[error,setError]=useState(''),[busy,setBusy]=useState(false),[reload,setReload]=useState(0)
  useEffect(()=>setAddress(active?.url||''),[active?.id])
  const open=async()=>{setBusy(true);setError('');try{const url=new URL(address);if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('请使用不含凭据的 HTTP(S) 地址。');if(url.origin===location.origin)throw Error('Morning 自己的地址无需再嵌入；可使用其他本地服务端口。');const item=await workspaceCommand<PreviewRecord>('preview_open',{url:url.href,session_id:data.session_id});refresh();onSelect(item.id)}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <div className="task-preview-pane">
    {!!data.previews.length&&<div className="work-resource-toolbar"><div role="tablist" aria-label="任务预览">{data.previews.map(item=><button type="button" role="tab" key={item.id} aria-selected={active?.id===item.id} title={item.url} onClick={()=>onSelect(item.id)}>{item.title}</button>)}</div></div>}
    <form className="preview-address" onSubmit={event=>{event.preventDefault();void open()}}><input aria-label="预览网址" placeholder="http://localhost:…" value={address} onChange={event=>setAddress(event.target.value)}/><button type="submit" className="icon-button" disabled={busy||!address.trim()||!data.session_id} aria-label="打开网址预览"><Plus size={14}/></button>{active&&<><button type="button" className="icon-button" aria-label="刷新预览" onClick={()=>setReload(value=>value+1)}><RefreshCw size={13}/></button><a className="icon-button" aria-label="在新标签打开预览" href={active.url} target="_blank" rel="noopener noreferrer"><ArrowUpRight size={14}/></a><button type="button" className="icon-button" aria-label="关闭这个预览" onClick={()=>void workspaceCommand('preview_remove',{id:active.id,session_id:data.session_id}).then(refresh).catch(e=>setError(e.message))}><X size={13}/></button></>}</form>
    {error&&<p className="work-error" role="alert">{error}</p>}
    {active?<><iframe key={active.id+':'+reload} title="任务网页预览" src={active.url} sandbox="allow-scripts allow-forms allow-popups" referrerPolicy="no-referrer" onError={()=>setError('页面没有载入，可在新标签中打开。')}/></>:<div className="work-empty"><p>暂无预览</p></div>}
  </div>
}
