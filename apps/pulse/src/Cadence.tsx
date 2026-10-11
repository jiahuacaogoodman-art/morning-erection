/* Cadence — the polling back-off, drawn.
   The kernel grows a watch's check interval by compounding ×1.5 while nothing changes
   (kernel/scheduling/adaptive.py), doubling it on failure, and dropping it straight back to
   the floor the moment something changes. Those two curves are what the 最短/最长 minutes
   above actually mean, so this draws them from the same recurrence rather than decorating.

   Pure SVG, no chart library: one path per rhythm, a band for the deterministic jitter,
   and a focusable dot per step so the numbers are reachable without a pointer.
*/
import { useId, useState } from 'react'

interface CadenceProps {
  minSeconds: number
  maxSeconds: number
}

const W = 640
const H = 152
const PAD = {left: 46, right: 16, top: 14, bottom: 24}
const PLOT_W = W - PAD.left - PAD.right
const PLOT_H = H - PAD.top - PAD.bottom
// The kernel's own default (adaptive.py: polling_policy seeds jitter_percent=10). Shown as a
// representative band, not a promise: a task can carry its own value.
const JITTER = 0.10

/** Quiet rhythm: ceil(current × 1.5) compounded from the floor, clamped at the ceiling.
    Mirrors `min(max_seconds, math.ceil(current * 1.5))` — the `adaptive:quiet` branch. */
function quietSteps(min: number, max: number): number[] {
  const steps: number[] = []
  let current = min
  for (let i = 0; i < 60; i++) {
    if (current >= max) break
    current = Math.min(max, Math.ceil(current * 1.5))
    steps.push(current)
  }
  if (steps.length === 0) steps.push(min)
  return [min, ...steps]
}

/** Failure rhythm: current × 2 compounded — the `adaptive:<error>` branch. */
function errorSteps(min: number, max: number): number[] {
  const ceiling = Math.max(900, max)
  const steps: number[] = []
  let current = min
  for (let i = 0; i < 60; i++) {
    if (current >= ceiling) break
    current = Math.min(ceiling, current * 2)
    steps.push(current)
  }
  if (steps.length === 0) steps.push(min)
  return [min, ...steps]
}

function seconds(v: number): string {
  if (v < 60) return `${Math.round(v)}秒`
  if (v < 3600) return `${(v / 60).toFixed(v % 60 ? 1 : 0)}分`
  return `${(v / 3600).toFixed(v % 3600 ? 1 : 0)}小时`
}

export function Cadence({minSeconds, maxSeconds}: CadenceProps) {
  const titleId = useId()
  const [hover, setHover] = useState<number | null>(null)

  const min = Math.max(2, Math.min(minSeconds, maxSeconds))
  const max = Math.max(min, maxSeconds)
  const quiet = quietSteps(min, max)
  const errors = errorSteps(min, max)
  const count = Math.max(quiet.length, errors.length)
  const ceiling = errors[errors.length - 1]
  // A log-ish scale: these spans run from 10s to 15min, and a linear axis would flatten
  // the first half of the curve into the baseline.
  const y = (v: number) => PAD.top + PLOT_H * (1 - Math.log(Math.max(v, 1)) / Math.log(Math.max(ceiling, 2)))
  const x = (i: number) => PAD.left + (count <= 1 ? 0 : (PLOT_W * i) / (count - 1))

  const line = (values: number[]) => values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(' ')
  // Jitter band: the kernel can add up to 10% on top of each interval.
  const band = `${line(quiet)} ${[...quiet].reverse().map((v, i) => `L${x(quiet.length - 1 - i).toFixed(1)} ${y(v * (1 + JITTER)).toFixed(1)}`).join(' ')} Z`
  const bandError = `${line(errors)} ${[...errors].reverse().map((v, i) => `L${x(errors.length - 1 - i).toFixed(1)} ${y(v * (1 + JITTER)).toFixed(1)}`).join(' ')} Z`

  const gridlines = [min, max]

  return <figure className="cadence-chart" aria-labelledby={titleId}>
    <figcaption id={titleId} className="cadence-caption">
      <span className="cadence-legend"><i className="cadence-swatch quiet"/>没有变化 · 逐步放慢</span>
      <span className="cadence-legend"><i className="cadence-swatch error"/>读取失败 · 更谨慎</span>
      <span className="cadence-legend"><i className="cadence-swatch reset"/>有变化 · 立刻回到 {seconds(min)}</span>
    </figcaption>
    <svg viewBox={`0 0 ${W} ${H}`} role="img"
      aria-label={`检查间隔的增长曲线：最短 ${seconds(min)}，安静时逐步放慢到最长 ${seconds(max)}，读取失败时更慢，出现变化立刻回到最短。`}
      onMouseLeave={() => setHover(null)}>
      {gridlines.map(v => <g key={v}>
        <line x1={PAD.left} x2={W - PAD.right} y1={y(v)} y2={y(v)} className="cadence-grid"/>
        <text x={PAD.left - 9} y={y(v) + 3.5} className="cadence-axis" textAnchor="end">{seconds(v)}</text>
      </g>)}

      <path d={bandError} className="cadence-band error"/>
      <path d={band} className="cadence-band quiet"/>
      <path d={line(errors)} className="cadence-line error"/>
      <path d={line(quiet)} className="cadence-line quiet"/>

      {/* the reset: this is what "出现变化" looks like on the interval, not just a caption */}
      <g className="cadence-reset">
        <path d={`M${PAD.left} ${y(min)} L${PAD.left} ${PAD.top + 6}`} />
        <circle cx={PAD.left} cy={y(min)} r="3.5"/>
      </g>

      {quiet.map((v, i) => <g key={i} className="cadence-node" tabIndex={0}
        onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)}
        role="img" aria-label={`第 ${i + 1} 次检查，间隔约 ${seconds(v)}`}>
        <circle cx={x(i)} cy={y(v)} r="7.5" className="cadence-hit"/>
        <circle cx={x(i)} cy={y(v)} r={hover === i ? 3.4 : 2.2} className="cadence-dot"/>
      </g>)}
    </svg>
    <p className="cadence-readout" aria-live="polite">
      {hover === null
        ? <>安静时每次检查间隔最多长到 <strong>{seconds(max)}</strong>，出现变化立刻回到 <strong>{seconds(min)}</strong>。</>
        : <>第 {hover + 1} 次检查，间隔约 <strong>{seconds(quiet[hover])}</strong>（加上不超过 10% 的随机抖动）。</>}
    </p>
  </figure>
}
