import type {ReactNode} from 'react'
import {Info} from 'lucide-react'

// Native disclosure keeps help available to touch, keyboard and screen readers.
export function FieldHelp({label,children}:{label:string;children:ReactNode}) {
  return <details className="field-help"><summary aria-label={`${label}说明`} title={`${label}说明`}><Info size={13}/></summary><div className="field-help-content">{children}</div></details>
}
