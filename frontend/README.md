# Центр-инвест — Liquidity frontend (prototype)

React + Vite + TypeScript, Tailwind v4, ECharts, TanStack Query, React Router, date-fns.
Frontend only: all data comes from `src/mocks` through `src/lib/api.ts`.

    npm install
    npm run dev      # http://localhost:5173
    npm run build

Connecting Django later: set `VITE_API_URL`; `src/lib/api.ts` then calls `fetch` instead of the mocks.
Response shapes are in `src/types/index.ts`. In Settings, "Состояние данных" forces loading / error / empty / incomplete states.
