"""CLI: создать оператора платформы. Пароль запрашивается интерактивно (не из аргументов и не из окружения).

    cd backend/services && BACKOFFICE_DB=/path/to/backoffice.db uv run python -m backoffice.create_operator operator@example.com
В Docker: docker compose exec backoffice python -m backoffice.create_operator operator@example.com
"""
import getpass
import sys

from . import store


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1].startswith("-"):
        print("usage: python -m backoffice.create_operator <email>", file=sys.stderr)
        return 2
    email = argv[1]
    try:
        store.normalize_email(email)
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
