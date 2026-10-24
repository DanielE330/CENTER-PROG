# Центр-инвест — фронтенд кассового прогноза

React 19 + Vite + TypeScript, Tailwind v4, TanStack Query, React Router, ECharts, i18next (RU/EN), светлая и тёмная темы.
Все данные приходят из публичного API бэкенда (`docs/knowledge-base.md` §5); моков нет.

    npm install
    npm run dev      # http://localhost:5173, /api проксируется на http://localhost:3000
    npm run build    # tsc --noEmit && vite build -> dist/ (раздаётся Rust API, WEB_DIR=frontend/dist)

Бэкенд и остальные сервисы: `infra/scripts/dev.sh` (из корня репозитория). Тот же origin, cookie `session`,
суммы — строки в копейках (BigInt), ошибки `{"error":{"code","message"}}`.

Структура: `src/lib/api.ts` (клиент), `src/hooks/queries.ts` (запросы и мутации), `src/types` (типы API),
`src/features/*` (страницы), `src/components` (ui и общие компоненты), `src/i18n` (строки RU/EN),
`public/cadesplugin_api.js` + `src/lib/cades.ts` (вход по ЭЦП, КриптоПро Browser plug-in).
