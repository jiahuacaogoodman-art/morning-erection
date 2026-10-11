import { Children, isValidElement, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import type { Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy } from 'lucide-react'

export function CopyButton({text, label = '复制回复'}: {text: string; label?: string}) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  useEffect(() => {if (state === 'idle') return; const timer = setTimeout(() => setState('idle'), 2000); return () => clearTimeout(timer)}, [state])
  return <button className="copy-button" type="button" aria-label={state === 'copied' ? '已复制' : state === 'failed' ? '复制失败，请手动选择' : label}
    title={label} onClick={() => {void navigator.clipboard.writeText(text).then(() => setState('copied'), () => setState('failed'))}}>
    {state === 'copied' ? <Check size={14}/> : <Copy size={14}/>}<span>{state === 'copied' ? '已复制' : state === 'failed' ? '复制失败' : label === '复制代码' ? '复制' : ''}</span>
  </button>
}

function CodeBlock({children}: {children?: ReactNode}) {
  const code = Children.toArray(children)[0]
  const props = isValidElement<{children?: ReactNode; className?: string}>(code) ? code.props : null
  const text = typeof props?.children === 'string' ? props.children : ''
  const language = props?.className?.replace('language-', '') || '代码'
  return <div className="code-block"><div className="code-toolbar"><span>{language}</span><CopyButton text={text} label="复制代码"/></div><pre>{children}</pre></div>
}

const components: Components = {
  pre: ({children}) => <CodeBlock>{children}</CodeBlock>,
  table: ({children}) => <div className="markdown-table" tabIndex={0} role="region" aria-label="表格"><table>{children}</table></div>,
  a: ({children, href}) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
}

export function Markdown({text}: {text: string}) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={components} skipHtml>{text}</ReactMarkdown></div>
}
