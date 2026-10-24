"""CLI: создать оператора платформы. Пароль запрашивается интерактивно (не из аргументов и не из окружения).

    cd backend/services && BACKOFFICE_DB=/path/to/backoffice.db uv run python -m backoffice.create_operator operator@example.com
В Docker: docker compose exec backoffice python -m backoffice.create_operator operator@example.com

Без терминала (install.sh): пароль одной строкой со стандартного ввода, а не из аргументов и не из окружения:
    printf '%s\n' "$PASSWORD" | docker compose exec -T backoffice python -m backoffice.create_operator --stdin operator@example.com
Сброс второго фактора (потерян телефон и резервные коды; при следующем входе настройка пройдёт заново):
    docker compose exec backoffice python -m backoffice.create_operator --reset-2fa operator@example.com
"""
import getpass
import sys

from . import store


def main(argv: list[str]) -> int:
    flags = [a for a in argv[1:] if a.startswith("-")]
    args = [a for a in argv[1:] if not a.startswith("-")]
    if len(args) != 1 or any(f not in ("--stdin", "--reset-2fa") for f in flags) or len(flags) > 1:
        print("usage: python -m backoffice.create_operator [--stdin | --reset-2fa] <email>", file=sys.stderr)
        return 2
    email = args[0]
    if flags == ["--reset-2fa"]:
        if not store.reset_mfa(email):
            print("Оператор не найден", file=sys.stderr)
            return 1
        print(f"Двухфакторная защита сброшена: {email.strip().lower()}. Настройка пройдёт при следующем входе.")
        return 0
    try:
        store.normalize_email(email)
        if flags == ["--stdin"]:
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = getpass.getpass(f"Пароль для {email} ({store.MIN_PASSWORD}–{store.MAX_PASSWORD} символов): ")
            if password != getpass.getpass("Повторите пароль: "):
                print("Пароли не совпадают", file=sys.stderr)
                return 1
        op = store.create_operator(email, password)
    except store.StoreError as e:
        msg = {
            "invalid_email": "Некорректный email",
            "invalid_password": f"Пароль должен быть от {store.MIN_PASSWORD} до {store.MAX_PASSWORD} символов",
            "email_taken": "Оператор с таким email уже есть",
        }.get(e.code, e.code)
        print(msg, file=sys.stderr)
        return 1
    print(f"Оператор создан: {op['email']} (БД: {store.DB_PATH})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
