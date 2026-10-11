import {useEffect, useId, useRef, useState} from 'react'
import type {ReactNode} from 'react'
import gsap from 'gsap'
import {useGSAP} from '@gsap/react'
import {ChevronDown} from 'lucide-react'

gsap.registerPlugin(useGSAP)
export {gsap, useGSAP}

export function useReducedMotion() {
  const [reduced, setReduced] = useState(() => matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {const media = matchMedia('(prefers-reduced-motion: reduce)'); const update = () => setReduced(media.matches); media.addEventListener('change', update); return () => media.removeEventListener('change', update)}, [])
  return reduced
}

export function Disclosure({title, hint, children, defaultOpen = false, open: controlled, onOpenChange, className = ''}: {
  title: ReactNode; hint?: ReactNode; children: ReactNode; defaultOpen?: boolean; open?:boolean; onOpenChange?:(open:boolean)=>void; className?:string
}) {
  const [internal, setInternal] = useState(defaultOpen)
  const open = controlled ?? internal
  const id = useId(), body = useRef<HTMLDivElement>(null), content = useRef<HTMLDivElement>(null)
  const reduced = useReducedMotion(), first = useRef(true)
  const {contextSafe} = useGSAP(() => {
    if (!body.current) return
    const immediate = first.current || reduced
    first.current = false
    gsap.to(body.current, {height:open ? content.current?.scrollHeight || 0 : 0, opacity:open ? 1 : 0,
      duration:immediate ? 0 : open ? .28 : .34,ease:open?'power3.out':'power4.inOut',overwrite:true,
      onComplete:() => {if (open && body.current) gsap.set(body.current,{height:'auto'})}})
  }, {scope:body,dependencies:[open,reduced]})
  const toggle = contextSafe(() => {
    if (body.current && !open) gsap.set(body.current,{height:0})
    if (body.current && open) gsap.set(body.current,{height:body.current.getBoundingClientRect().height})
    if (controlled === undefined) setInternal(!open)
    onOpenChange?.(!open)
  })
  return <section className={`disclosure ${open ? 'is-open' : ''} ${className}`}>
    <button type="button" className="disclosure-trigger" aria-expanded={open} aria-controls={id} onClick={toggle}>
      <span><span className="disclosure-title">{title}</span>{hint && <span className="disclosure-hint">{hint}</span>}</span><ChevronDown size={15}/>
    </button>
    <div id={id} ref={body} className="disclosure-body" inert={!open} aria-hidden={!open} style={{height:defaultOpen ? 'auto' : 0, overflow:'hidden'}}>
      <div ref={content} className="disclosure-content">{children}</div>
    </div>
  </section>
}

export function SlidingTabs({items,value,onChange,label}:{items:{id:string;label:string}[];value:string;onChange:(value:string)=>void;label:string}) {
  const root=useRef<HTMLDivElement>(null),indicator=useRef<HTMLSpanElement>(null),reduced=useReducedMotion(),first=useRef(true)
  const align=(duration:number)=>{const target=root.current?.querySelector<HTMLElement>('[data-tab-selected="true"]');if(target&&indicator.current)gsap.to(indicator.current,{x:target.offsetLeft,y:target.offsetTop,width:target.offsetWidth,height:target.offsetHeight,opacity:1,duration,ease:'power3.inOut',overwrite:true})}
  const {contextSafe}=useGSAP(()=>{align(first.current||reduced?0:.28);first.current=false},{scope:root,dependencies:[value,items.map(item=>item.id).join(','),reduced]})
  useEffect(()=>{if(!root.current)return;const resize=new ResizeObserver(contextSafe(()=>align(0)));resize.observe(root.current);return()=>resize.disconnect()},[])
  return <div ref={root} className="settings-subnav sliding-tabs" role="group" aria-label={label}><span ref={indicator} className="sliding-tab-indicator" aria-hidden="true"/>{items.map(item=><button type="button" key={item.id} aria-pressed={value===item.id} data-tab-selected={value===item.id} className={value===item.id?'selected':''} onClick={()=>onChange(item.id)}>{item.label}</button>)}</div>
}
