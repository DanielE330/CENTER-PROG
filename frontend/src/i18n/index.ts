import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import ru from './locales/ru.json'
import en from './locales/en.json'

export type Language = 'ru' | 'en'
export const LANGUAGES: Language[] = ['ru', 'en']

const KEY = 'ci.lang'

/** Russian is the default; only an explicit saved choice switches to English. */
function initialLanguage(): Language {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'en' || saved === 'ru') return saved
  } catch {
    /* storage unavailable */
  }
  return 'ru'
}

void i18n.use(initReactI18next).init({
  resources: { ru: { translation: ru }, en: { translation: en } },
  lng: initialLanguage(),
  fallbackLng: 'ru',
  supportedLngs: LANGUAGES,
  keySeparator: '.',
  nsSeparator: false,
  interpolation: { escapeValue: false },
  returnNull: false,
})

function syncDocument() {
  document.documentElement.lang = i18n.language
  document.title = i18n.t('app.title')
}
syncDocument()
i18n.on('languageChanged', syncDocument)

export function setLanguage(lng: Language) {
  void i18n.changeLanguage(lng)
  try {
    localStorage.setItem(KEY, lng)
  } catch {
    /* ignore: language still applies for this session */
  }
}

export default i18n
