// A browser tab keeps its selected view while other Pi sessions continue running.
let sessionId=sessionStorage.getItem('morning:pi-session')||undefined
export const getPiSessionId=()=>sessionId
export function setPiSessionId(value:string|undefined){
  sessionId=value
  if(value)sessionStorage.setItem('morning:pi-session',value)
  else sessionStorage.removeItem('morning:pi-session')
}
