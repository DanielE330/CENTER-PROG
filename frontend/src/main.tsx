import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/inter/cyrillic-400.css'
import '@fontsource/inter/cyrillic-500.css'
import '@fontsource/inter/cyrillic-600.css'
import '@fontsource/inter/cyrillic-700.css'
import '@fontsource/inter/latin-400.css'
import '@fontsource/inter/latin-500.css'
import '@fontsource/inter/latin-600.css'
import '@fontsource/inter/latin-700.css'
import './i18n'
import App from './App'
import './styles/globals.css'

// После выката имена чанков меняются: устаревшая вкладка не находит старый модуль. Один раз перезагружаемся за новой версией.
window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault()
  try {
    if (sessionStorage.getItem('chunk-reload') === '1') return
    sessionStorage.setItem('chunk-reload', '1')
  } catch { /* storage недоступен: просто перезагрузим */ }
  window.location.reload()
})
// сбрасываем флаг только через 10 с после загрузки, чтобы при реально отсутствующем чанке не уйти в цикл перезагрузок
window.addEventListener('load', () => setTimeout(() => { try { sessionStorage.removeItem('chunk-reload') } catch { /* ignore */ } }, 10000))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
