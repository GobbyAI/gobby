import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '@fontsource-variable/jetbrains-mono'
import './styles/index.css'
import './styles/buttons.css'
import './styles/segmented-control.css'
import './styles/app-shell.css'
import './styles/settings.css'
import './styles/settings-overlay.css'
import App from './App'
import { AppErrorBoundary } from './components/app/AppErrorBoundary'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppErrorBoundary
      activeTab="application"
      onReturnToChat={() => {
        window.location.reload()
      }}
    >
      <App />
    </AppErrorBoundary>
  </StrictMode>,
)
