/* Fuzzy animated status orb — hand-written SVG filter + CSS only, no canvas.
   State drives colour and rhythm; prefers-reduced-motion cuts to a static gradient.

   data-state values:
     idle      — quiet, slow grey-green breathing
     working   — fast blue pulse
     approval  — amber with halo flare
     error     — red
     offline   — desaturated grey, no animation
*/
import { useId } from 'react'

export type OrbState = 'idle' | 'working' | 'approval' | 'error' | 'offline'

interface OrbProps {
  state?: OrbState
  size?: number
  /** Aria label for the text fallback. The SVG itself is aria-hidden. */
  label?: string
  /** Override the hue (0–359) of every coloured state, keeping the state's own lightness,
      chroma and rhythm. `null`/omitted keeps the built-in palette exactly as-is.
      `offline` is deliberately exempt — being disconnected should not be tinted. */
  hue?: number | null
}

// Each state: [core L/C, halo L/C, animation speed multiplier, base hue].
// Hue lives apart from the rest so a user-chosen hue can replace it without disturbing
// the per-state weight (a `working` orb stays the most saturated, `idle` the softest).
const STATE_COLOURS: Record<OrbState, [string, string, number, number]> = {
  idle:     ['oklch(62% 0.09 %h)', 'oklch(72% 0.07 %h)', 1,    155],
  working:  ['oklch(58% 0.18 %h)', 'oklch(68% 0.14 %h)', 0.45, 255],
  approval: ['oklch(65% 0.15 %h)', 'oklch(75% 0.13 %h)', 0.6,  75],
  error:    ['oklch(55% 0.20 %h)', 'oklch(65% 0.16 %h)', 0.5,  25],
  offline:  ['oklch(55% 0.02 %h)', 'oklch(65% 0.02 %h)', 0,    0],
}

/** The user's hue wins for every state that carries colour; `offline` keeps its grey. */
export function hueOf(state: OrbState, hue: number | null | undefined): number {
  const base = STATE_COLOURS[state][3]
  if (hue == null || state === 'offline') return base
  return hue
}

/** Palette as [core, halo, speed] for one state under an optional hue override. */
export function orbColours(state: OrbState, hue: number | null | undefined): [string, string, number] {
  const [core, halo, speed, base] = STATE_COLOURS[state]
  if (hue == null || state === 'offline') return [core.replace('%h', String(base)), halo.replace('%h', String(base)), speed]
  return [core.replace('%h', String(hue)), halo.replace('%h', String(hue)), speed]
}

/** oklch() as a plain colour, for previews that sit outside an SVG. */
export function orbCss(state: OrbState, hue: number | null | undefined, which: 0 | 1 = 0): string {
  return orbColours(state, hue)[which] as string
}

export function Orb({state = 'idle', size = 12, label, hue}: OrbProps) {
  const filterId = useId()
  const [core, halo, speed] = orbColours(state, hue)
  // animation duration in seconds: base 3.6s / speed, 0 speed = no animation (reduced-motion path)
  const dur = speed > 0 ? (3.6 / speed).toFixed(2) : '0'
  const r = size / 2

  return (
    <span className="orb-wrap" data-state={state} style={{'--orb-size': `${size}px`} as React.CSSProperties}>
      {/* Screen reader text — the SVG below is presentation only */}
      {label && <span className="sr-only">{label}</span>}

      <svg
        aria-hidden="true"
        focusable="false"
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        overflow="visible"
        className="orb-svg"
      >
        <defs>
          {/* feTurbulence gives the fuzzy, organic edge — the seed animates slowly so
              the boundary keeps breathing rather than being a static noise shape. */}
          <filter id={filterId} x="-40%" y="-40%" width="180%" height="180%" colorInterpolationFilters="sRGB">
            <feTurbulence
              type="fractalNoise"
              baseFrequency="0.65"
              numOctaves="3"
              seed="2"
              result="noise"
            >
              {/* Only animate turbulence when motion is allowed — the @media check below
                  handles reduced-motion; we also guard with dur>0. */}
              {speed > 0 && (
                <animate
                  attributeName="seed"
                  values="2;8;14;2"
                  dur={`${(parseFloat(dur) * 4).toFixed(2)}s`}
                  repeatCount="indefinite"
                />
              )}
            </feTurbulence>
            <feDisplacementMap
              in="SourceGraphic"
              in2="noise"
              scale={size * 0.3}
              xChannelSelector="R"
              yChannelSelector="G"
              result="fuzzy"
            />
            {/* Soft glow — composited on top of the displaced shape */}
            <feGaussianBlur in="fuzzy" stdDeviation={size * 0.08} result="glow" />
            <feMerge>
              <feMergeNode in="glow" />
              <feMergeNode in="fuzzy" />
            </feMerge>
          </filter>

          {/* Core radial gradient — the orb fill */}
          <radialGradient id={`${filterId}-g`} cx="38%" cy="35%" r="60%">
            <stop offset="0%" stopColor={halo} />
            <stop offset="100%" stopColor={core} />
          </radialGradient>
        </defs>

        {/* Outer halo ring — phase-offset breathing */}
        <circle
          cx={r} cy={r} r={r * 1.35}
          fill="none"
          stroke={halo}
          strokeWidth={size * 0.06}
          opacity="0"
          className="orb-halo"
        />

        {/* The orb itself, filtered for fuzzy edge */}
        <circle
          cx={r} cy={r} r={r * 0.82}
          fill={`url(#${filterId}-g)`}
          filter={`url(#${filterId})`}
          className="orb-core"
        />
      </svg>
    </span>
  )
}
