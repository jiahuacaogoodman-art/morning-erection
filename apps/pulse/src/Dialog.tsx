import {useEffect, useId, useRef} from 'react'
import type {ReactNode} from 'react'
import {X} from 'lucide-react'

export function Dialog({title, children, close, wide = false, eyebrow = 'MORNING'}: {title: string; children: ReactNode; close: () => void; wide?: boolean; eyebrow?: string}) {
  const ref = useRef<HTMLDivElement>(null), id = useId(), closeRef = useRef(close)
  closeRef.current = close
  useEffect(() => {
    const previous = document.activeElement as HTMLElement
    const focusable = () => Array.from(ref.current?.querySelectorAll<HTMLElement>('button,input,textarea,select,a[href],[tabindex="0"]') || [])
      .filter(el => !el.hasAttribute('disabled') && el.getClientRects().length > 0)
    focusable()[0]?.focus()
    const handle = (event: KeyboardEvent) => {
      if (Array.from(document.querySelectorAll('[role="dialog"]')).at(-1) !== ref.current) return
      if (event.key === 'Escape') {event.preventDefault(); closeRef.current()}
      if (event.key === 'Tab') {
        const elements = focusable(), first = elements[0], last = elements.at(-1)
        if (event.shiftKey && document.activeElement === first) {event.preventDefault(); last?.focus()}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first?.focus()}
      }
    }
    document.addEventListener('keydown', handle)
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {document.removeEventListener('keydown', handle); document.body.style.overflow = overflow; previous?.focus()}
  }, [])
  return <div className="dialog-scrim" onMouseDown={e => {if (e.target === e.currentTarget) close()}}>
    <div ref={ref} className={`drawer ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby={id}>
      <header className="drawer-header"><div><span className="eyebrow">{eyebrow}</span><h2 id={id}>{title}</h2></div>
        <button className="icon-button" aria-label="关闭" onClick={close}><X size={18}/></button></header>{children}
    </div>
  </div>
}
