import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '@fontsource-variable/jetbrains-mono'
import './styles/index.css'
import './styles/buttons.css'
import './styles/settings.css'
import './styles/source-control.css'
import './styles/source-control-diff.css'
import './styles/source-control-issues.css'
import App from './App'

createRoot(document.getElementById('root')!).render(<App />)
