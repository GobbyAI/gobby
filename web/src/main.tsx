import { createRoot } from 'react-dom/client'
import './styles/index.css'
import './styles/buttons.css'
import './styles/settings.css'
import './styles/source-control.css'
import App from './App'

createRoot(document.getElementById('root')!).render(<App />)
