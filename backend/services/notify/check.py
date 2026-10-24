"""Проверка почты из консоли: python -m notify.check [--to адрес]

Без --to: соединение, EHLO, STARTTLS, вход и проверка отправителя, письмо не уходит.
С --to: после успешной проверки отправляется настоящее тестовое письмо на этот адрес (напрямую, мимо очереди).
В контейнере: docker compose -f infra/docker-compose.yml exec notify python -m notify.check --to me@example.ru
Код выхода 0 — всё хорошо, 1 — ошибка, 2 — неверные аргументы. Пароль не печатается."""

import argparse
import sys

from . import mailer, render

STAGES = {
    "config": "настройки SMTP_*",
    "connect": "соединение с сервером",
    "ehlo": "приветствие EHLO",
    "starttls": "включение шифрования STARTTLS",
    "auth": "вход по логину и паролю",
    "sender": "проверка отправителя",
    "send": "отправка письма",
    "done": "готово",
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m notify.check", description=__doc__.split("\n")[0])
    ap.add_argument("--to", help="после проверки отправить тестовое письмо на этот адрес")
    args = ap.parse_args(argv)
    res = mailer.check()
    if res.get("host"):
        print(f"Сервер:      {res['host']}:{res['port']}, шифрование {res['security']}, EHLO {res['helo']}")
        print(f"Вход:        {'по логину и паролю' if res['user_set'] else 'без входа'}")
        print(f"Отправитель: {res['sender']}")
    for w in res.get("warnings", []):
        print(f"Внимание: {w}")
    if not res["ok"]:
        detail = f" ({res['detail']})" if res["detail"] != res["error_class"] else ""
        print(f"ОШИБКА на шаге «{STAGES.get(res['stage'], res['stage'])}»: {res['error_class']}{detail}")
        print(f"Что делать: {res['hint']}")
        return 1
    print("Проверка пройдена: соединение, шифрование, вход и отправитель приняты сервером.")
    if not args.to:
        return 0
    to = args.to.strip()
    if "@" not in to:
        print("Адрес получателя указан неверно")
        return 2
    cfg = mailer.config()
    subject, html, text, _ = render.render("test", {"email": to})
    row = {"to_addr": to, "subject": subject, "html": html, "text": text, "headers": ""}
    try:
        mailer.deliver(mailer.build(row, cfg["sender"]), cfg)
    except Exception as e:  # noqa: BLE001
        info = mailer.classify(e, cfg)
        print(f"ОШИБКА при отправке письма: {mailer.describe(info)}")
        print(f"Что делать: {info['hint']}")
        return 1
    print("Тестовое письмо принято сервером. Если оно не пришло, посмотрите папку «Спам» и журнал провайдера.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
