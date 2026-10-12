"""Бот создаёт групповой чат организации (сам становится админом) и добавляет туда людей.

    python create_chat.py --name "Созвоны с Клодом" you@your-domain.ru [other@your-domain.ru ...]
"""

import argparse
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

ap = argparse.ArgumentParser()
ap.add_argument("--name", default="Созвоны с Клодом")
ap.add_argument("logins", nargs="+", help="логины сотрудников организации (user@domain)")
args = ap.parse_args()

users = [{"login": login} for login in args.logins]
r = requests.post(
    "https://botapi.messenger.yandex.net/bot/v1/chats/create/",
    headers={"Authorization": f"OAuth {os.environ['YANDEX_BOT_TOKEN']}"},
    json={"name": args.name, "description": "", "admins": users, "members": users},
    timeout=30,
)
print(r.status_code, r.text)
