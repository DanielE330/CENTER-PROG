# Среда разработки CenterInvest

Инструменты подготовлены на этом сервере. Реализация приложения и deploy не запускались.

## Запуск

```bash
cd /root/Projects/Hackathon/CenterInvest
source .venv/bin/activate
claude
```

Python-библиотеки находятся в `.venv`, не в системном Python. Установлен CPU PyTorch: GPU на сервере не обнаружен. CUDA и драйверы не устанавливались.

Frontend-инструменты пока изолированы от будущего приложения:

```bash
pnpm --dir .tooling/frontend exec tsc --version
pnpm --dir .tooling/frontend exec vite --version
pnpm --dir .tooling/frontend exec vitest --version
pnpm --dir .tooling/frontend exec playwright --version
```

При создании `apps/web` зависимости нужно явно добавить в его package.json и lockfile. Каталог `.tooling/frontend` не является приложением или scaffold проекта.

## Скиллы

11 скиллов установлены локальными копиями в `.claude/skills/`. Источники сохранены установщиком в skills-lock.json. Проверка списка:

```bash
npx skills list --agent claude-code
```

- rust-best-practices
- pytorch
- ansible
- vercel-react-best-practices
- frontend-design
- webapp-testing
- github-actions-templates
- security-requirement-extraction
- docker-expert
- semgrep
- systematic-debugging

## Проверено

- Rust: компиляция, cargo fmt, Clippy, 2 теста в nextest, llvm-cov на отдельном smoke crate.
- React/TypeScript: typecheck и production build через Vite на изолированном smoke-примере.
- PyTorch: вычисление и autograd на CPU.
- Playwright: запуск Chromium и чтение отрисованного DOM.
- Python: uv pip check, зависимости совместимы.
- CLI: версии установленных Rust, инфраструктурных и security-инструментов.
- Podman: info и наличие Quadlet generator. Проверка выполнена от root; rootless-пользователь приложения не создавался.
- Docker: доступен существующий daemon и Compose. Образ ZAP скачан, CLI запущен, версия 2.17.0.

Это проверка инструментов, а не security-аудит ещё не написанного приложения. Advisory-базы, правила сканеров, аутентификация gh и подпись реального релиза потребуют настройки по проекту.

## Контейнер ZAP

```bash
docker run --rm ghcr.io/zaproxy/zaproxy:stable zap.sh -cmd -version
```

Digest загруженного образа записан в `toolchain-versions.json`. Для воспроизводимого CI использовать digest, не плавающий tag.

PostgreSQL server, Caddy, Prometheus и Grafana не запускались. Docker не перезапускался. Порты приложения не открывались. Установлены PostgreSQL client и Podman, но не выполнялась настройка продуктивного стенда.

## Секреты

`git_token.md` не читался. Файл исключён из Git; в настройках Claude добавлен запрет прямого Read этого файла и .env. Это не sandbox для shell. Не выдавать агенту production credentials и не отключать все подтверждения.

На сервере около 4 GiB RAM и 2 CPU. Обучение, параллельные Rust builds и несколько security-сканеров одновременно могут исчерпать память.
