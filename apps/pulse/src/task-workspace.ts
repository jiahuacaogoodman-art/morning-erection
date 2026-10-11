import {useEffect,useState} from 'react'
import {api} from './api'
import {getPiSessionId} from './session-context'

export type WorkTab='computer'|'terminal'|'preview'
export type WorkTarget={kind:WorkTab;id?:string;app?:string;device?:string;display?:number}
export type TerminalRecord={id:string;title:string;state:string;exit_code:number|null;workspace:string;session_id:string;session_path?:string;created:number;updated:number;error?:string}
export type PreviewRecord={id:string;url:string;title:string;session_id:string}
export type WorkspaceState={available:boolean;session_id:string|null;workspace:string;terminals:TerminalRecord[];previews:PreviewRecord[];concurrency:{terminal_limit:number;desktop:string}}
export const workspaceCommand=<T,>(operation:string,params:Record<string,unknown>={},signal?:AbortSignal)=>api<T>('/pulse/workspace',{operation,...params},signal)

export function useTaskWorkspace(enabled:boolean,sessionId:string|undefined,open:boolean){
  const [data,setData]=useState<WorkspaceState|null>(null),[error,setError]=useState(''),[revision,setRevision]=useState(0)
  useEffect(()=>{
    if(!enabled){setData(null);return}
    let stopped=false,timer:ReturnType<typeof setTimeout>;const controller=new AbortController()
    const poll=async()=>{
      try{const next=await api<WorkspaceState>('/pulse/workspace',undefined,controller.signal);if(!stopped&&(!sessionId||next.session_id===sessionId)){setData(next);setError('')}}
      catch(e){if(!stopped)setError((e as Error).message)}
      finally{if(!stopped)timer=setTimeout(poll,open?1200:5000)}
    }
    void poll();return()=>{stopped=true;controller.abort();clearTimeout(timer)}
  },[enabled,sessionId,revision])
  // The polling timer can stay slow while the drawer is closed; changing tabs
  // does not recreate processes or reset their output.
  return {data:sessionId&&(data?.session_id!==sessionId||getPiSessionId()!==sessionId)?null:data,error,refresh:()=>setRevision(value=>value+1)}
}
