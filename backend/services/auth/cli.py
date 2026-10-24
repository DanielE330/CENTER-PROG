"""Служебные команды оператора: создание владельца с обязательной 2FA и сброс второго фактора.

Работают напрямую с БД auth (AUTH_DB), в сеть не выходят. Пароль берётся из переменной окружения
OPERATOR_PASSWORD, а не из аргументов командной строки (аргументы видны в списке процессов).

    python -m auth.cli create-operator --email admin@example.com      # пароль из OPERATOR_PASSWORD
    python -m auth.cli reset-2fa --email admin@example.com            # при следующем входе 2FA настраивается заново
"""
import argparse
import os
import secrets
import sys
import time

from . import app as auth_app


def create_operator(email: str, password: str, created: list | None = None) -> str:
    """Идемпотентно: существующему пользователю включает обязательную 2FA, пароль не меняет."""
    email = email.strip().lower()
    if not auth_app.EMAIL_RE.match(email):
        raise SystemExit("Некорректный email")
    if not (8 <= len(password) <= 128):
        raise SystemExit("Пароль должен быть от 8 до 128 символов")
    with auth_app.db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            uid = row[0]
        else:
            if created is not None:
                created.append(True)
            uid = secrets.token_hex(16)
            salt = secrets.token_bytes(16)
            conn.execute(
                "INSERT INTO users(id,email,salt,pwd_hash,created_at) VALUES(?,?,?,?,?)",
                (uid, email, salt, auth_app.hash_password(password, salt), time.time()),
            )
        conn.execute("UPDATE users SET mfa_required=1 WHERE id=?", (uid,))
    return uid


def reset_2fa(email: str) -> bool:
    """Снимает второй фактор и завершает сессии. Обязательность сохраняется: следующий вход начнётся с настройки."""
    with auth_app.db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
        if not row:
            return False
        uid = row[0]
        conn.execute("UPDATE users SET mfa_secret=NULL, mfa_enabled=0, mfa_last_step=0 WHERE id=?", (uid,))
        for t in ("mfa_backup", "mfa_pending", "sessions"):
            conn.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="auth.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("create-operator", "reset-2fa"):
        sp = sub.add_parser(name)
        sp.add_argument("--email", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "create-operator":
        pwd = os.environ.get("OPERATOR_PASSWORD", "")
        created: list = []
        create_operator(a.email, pwd, created)
        print("created" if created else "exists")  # у существующего пароль не меняется
        return 0
    if not reset_2fa(a.email):
        print("Пользователь не найден", file=sys.stderr)
        return 1
    print("2FA сброшена: при следующем входе её нужно настроить заново")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
