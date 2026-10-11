import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { applyCachedTheme } from './Settings'
import './style.css'
import './workspace.css'
import './thinking.css'

// Before the first paint, so a dark-theme user never sees a white flash.
applyCachedTheme()

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
