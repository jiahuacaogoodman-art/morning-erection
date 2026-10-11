import {useId, useMemo, useState} from 'react'
import type {AnimationEvent, CSSProperties} from 'react'
import sculpture from '../public/morning-logo.svg?raw'

type MorningMarkProps = {size?:number; active?:boolean; reveal?:boolean; interactive?:boolean; label?:string; className?:string}

/** One canonical layered sculpture for every brand surface; IDs are private per instance. */
export function MorningMark({size = 28, active = false, reveal = false, interactive = false, label, className = ''}: MorningMarkProps) {
  const instance = useId().replace(/[^a-zA-Z0-9_-]/g, '')
  const [replay, setReplay] = useState(0)
  const [entering, setEntering] = useState(reveal || interactive)
  // Keep the innerHTML payload stable across Pulse polling; otherwise React would
  // replace the SVG repeatedly and restart every layer's animation mid-flight.
  const artwork = useMemo(() => ({__html:sculpture.replaceAll('ml-', `morning-${instance}-`)}), [instance])
  const classes = `morning-logo${active ? ' is-active' : ''}${entering ? ' is-intro' : ''}${interactive ? ' is-interactive' : ''} ${className}`
  const style = {'--mark-size': `${size}px`} as CSSProperties
  // Markup is a checked-in SVG, never model, plugin, or user-supplied HTML.
  const art = <span key={replay} className="morning-logo-art" aria-hidden="true" dangerouslySetInnerHTML={artwork}/>
  const settled = (event:AnimationEvent<HTMLElement>) => {
    if (event.animationName === 'logo-sun-enter') setEntering(false)
  }
  return interactive
    ? <button type="button" className={classes} style={style} aria-label={label || '重播 Morning Logo 动画'} title="点击重播" onAnimationEnd={settled} onClick={() => {setEntering(true); setReplay(value => value + 1)}}>{art}</button>
    : <span className={classes} style={style} role={label ? 'img' : undefined} aria-label={label} aria-hidden={label ? undefined : true} onAnimationEnd={settled}>{art}</span>
}
