"""Manage Centro users without putting passwords in shell history."""
from __future__ import annotations

import argparse
import getpass

from app.services import centro_sales


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Управление пользователями базы Центробежные")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--username", required=True)
    create.add_argument("--role", choices=sorted(centro_sales.ROLES), default="sales")
    password = sub.add_parser("set-password")
    password.add_argument("--username", required=True)
    args = parser.parse_args(argv)
    centro_sales.init_schema()
    if args.command == "set-password":
        with centro_sales.connect() as conn:
            row = conn.execute("SELECT role FROM users WHERE username=?", (args.username,)).fetchone()
        if not row:
            parser.error("Пользователь не найден")
        role = row["role"]
    else:
        role = args.role
    first = getpass.getpass("Пароль: ")
    second = getpass.getpass("Повторите пароль: ")
    if first != second:
        parser.error("Пароли не совпадают")
    centro_sales.upsert_user(args.username, first, role)
    print(f"Пользователь {args.username} сохранён (роль: {role}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
