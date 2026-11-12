#!/usr/bin/env python3
"""Ежедневный сбор сообщений с домашними заданиями из Telegram-группы 5072.

Запускается на Zomro. Читает группу через сессию парсер-аккаунта Shiller (копия,
рабочая сессия не трогается), отбирает сообщения по ключевым словам и пишет
homework.json: только дата, время и ссылка на сообщение (текст не сохраняется,
репозиторий публичный).

Переменные окружения (необязательно):
  HW_CHAT      username группы (по умолчанию AI5072)
  HW_PHONE     номер парсер-аккаунта без + (по умолчанию 79254642868)
  HW_OUT       путь к homework.json (по умолчанию рядом со скриптом, на уровень выше)
  HW_STATE     файл состояния с id последнего обработанного сообщения
  HW_PUSH=1    после записи сделать git add/commit/push в репозитории HW_OUT
"""
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from pyrogram import Client

CHAT = os.environ.get("HW_CHAT", "AI5072")
PHONE = os.environ.get("HW_PHONE", "79254642868")
DATA_DIR = os.environ.get("HW_DATA_DIR", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("HW_OUT", os.path.join(HERE, "..", "homework.json"))
STATE = os.environ.get("HW_STATE", os.path.join(HERE, "hw_state.json"))
FIRST_RUN_DAYS = 14
MSK = timezone(timedelta(hours=3))

# ключевые слова: «дз», «домашка/домашнее/домашнюю», «задали», «задание»
KEYWORDS = re.compile(
    r"(?<![а-яa-z])(дз|д/з)(?![а-яa-z])|домашк|домашн|задали|задани",
    re.IGNORECASE,
)


def read_config():
    cfg = {}
    with open(os.path.join(DATA_DIR, "bot_config.txt"), encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def credentials(cfg):
    acc = None
    for a in json.loads(cfg.get("parser_accounts", "[]")):
        if a["phone"].replace("+", "") == PHONE:
            acc = a
    if acc is None:
        raise SystemExit("аккаунт %s не найден в parser_accounts" % PHONE)
    return int(acc.get("api_id", cfg.get("api_id"))), acc.get("api_hash", cfg.get("api_hash"))


def copy_session(workdir):
    """Консистентная копия сессии через sqlite backup (рабочая не блокируется)."""
    src = os.path.join(DATA_DIR, "pyro_parser_%s.session" % PHONE)
    dst = os.path.join(workdir, "hw.session")
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


async def fetch(api_id, api_hash, workdir, last_id):
    found = []
    max_id = last_id
    async with Client("hw", api_id=api_id, api_hash=api_hash, workdir=workdir, no_updates=True) as c:
        chat = await c.get_chat(CHAT)
        since = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_DAYS)
        async for m in c.get_chat_history(chat.id):
            if last_id and m.id <= last_id:
                break
            dt = m.date.replace(tzinfo=timezone.utc) if m.date.tzinfo is None else m.date
            if not last_id and dt < since:
                break
            max_id = max(max_id, m.id)
            text = (m.text or m.caption or "")
            if text and KEYWORDS.search(text):
                local = dt.astimezone(MSK)
                found.append({
                    "id": m.id,
                    "date": local.strftime("%Y-%m-%d"),
                    "time": local.strftime("%H:%M"),
                    "url": "https://t.me/%s/%d" % (CHAT, m.id),
                })
    return found, max_id


def push(path):
    repo = os.path.dirname(os.path.abspath(path))
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True)
    run("git", "pull", "--rebase", "--autostash")
    if not run("git", "status", "--porcelain", os.path.basename(path)).stdout.strip():
        return False
    run("git", "add", os.path.basename(path))
    run("git", "commit", "-m", "ДЗ 5072: обновление ссылок на сообщения")
    run("git", "push")
    return True


def main():
    cfg = read_config()
    api_id, api_hash = credentials(cfg)
    state = load_json(STATE, {})
    last_id = int(state.get("last_id", 0))
    with tempfile.TemporaryDirectory() as tmp:
        copy_session(tmp)
        found, max_id = asyncio.run(fetch(api_id, api_hash, tmp, last_id))

    items = load_json(OUT, {}).get("items", [])
    known = {i["id"] for i in items}
    added = [f for f in found if f["id"] not in known]
    items = sorted(items + added, key=lambda i: i["id"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"group": "5072 ПИ (ИИвЦЭ)", "items": items}, f, ensure_ascii=False, indent=1)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"last_id": max_id}, f)
    print("новых ДЗ: %d, всего: %d, last_id=%d" % (len(added), len(items), max_id))
    if os.environ.get("HW_PUSH") == "1":
        print("push:", "да" if push(OUT) else "изменений нет")


if __name__ == "__main__":
    sys.exit(main())
