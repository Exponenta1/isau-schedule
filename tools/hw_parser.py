#!/usr/bin/env python3
"""Ежедневный сбор домашних заданий из Telegram-группы 5072.

Запускается на Zomro. Читает группу через копию сессии парсер-аккаунта Shiller
(рабочая сессия не трогается) и находит сообщения с ДЗ:
  1. старосты размечают ДЗ хэштегами (#англ, #спсау, #сп #волкова ...) - главный сигнал;
  2. сообщение без хэштега, но со словами «дз/задание/домашка» и названием предмета
     или фамилией преподавателя в тексте.
Для каждого ДЗ определяется предмет (полное название из расписания), преподаватель,
если он указан, и явная дата сдачи, если она написана в тексте («к 18.11», «до 20 ноября»).
Дату самой пары считает страница расписания (учитывает отмены и переносы).

В публичный homework.json пишутся только предмет, даты и ссылка на сообщение,
текст сообщений не сохраняется.

Переменные окружения (необязательно):
  HW_CHAT, HW_PHONE, HW_DATA_DIR, HW_OUT, HW_STATE, HW_SCHEDULE (путь к index.html)
  HW_DAYS   глубина первого прохода в днях (по умолчанию 14)
  HW_PUSH=1 после записи сделать git add/commit/push в репозитории HW_OUT
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

from pyrogram import Client

CHAT = os.environ.get("HW_CHAT", "AI5072")
PHONE = os.environ.get("HW_PHONE", "79254642868")
GROUP = "5072 ПИ (ИИвЦЭ)"
DATA_DIR = os.environ.get("HW_DATA_DIR", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("HW_OUT", os.path.join(HERE, "..", "homework.json"))
STATE = os.environ.get("HW_STATE", os.path.join(HERE, "hw_state.json"))
SCHEDULE = os.environ.get("HW_SCHEDULE", os.path.join(HERE, "..", "index.html"))
FIRST_RUN_DAYS = int(os.environ.get("HW_DAYS", "14"))
MSK = timezone(timedelta(hours=3))

# слова ДЗ для сообщений без хэштега
HW_WORDS = re.compile(r"(?<![а-яa-z])(дз|д/з)(?![а-яa-z])|домашк|домашн|задали|задани|home ?task|homework",
                      re.IGNORECASE)

# (шаблон начала слова, часть названия предмета, фамилия преподавателя или None, только_хэштег)
# «только_хэштег»: короткие/многозначные слова, в обычном тексте не используются
ALIASES = [
    (r"англ|english", "Английский язык", None, False),
    (r"спсау|сакрюк", "Современные проблемы системного", None, False),
    (r"волк", "Стратегическое планирование", "Волкова", False),
    (r"шокин", "Стратегическое планирование", "Шокин", False),
    (r"стратег", "Стратегическое планирование", None, False),
    (r"сп$", "Стратегическое планирование", None, True),
    (r"иад|нейросет", "Интеллектуальный анализ данных", None, False),
    (r"кравец", "Интеллектуальный анализ данных", "Кравец", False),
    (r"иирбп|федоров|инжиниринг|реинжин", "Инжиниринг", None, False),
    (r"кцэ|тюпик", "Концепции цифровой экономики", None, False),
    (r"нир$|тятюш", "Научно-исследовательская", None, False),
    (r"этик|крейдер", "Профессиональная этика", None, False),
    (r"философ|билятд", "Философские вопросы", None, False),
]

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
URL = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)
TAG = re.compile(r"(?<![\w/#&=])#(\w+)")


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


def load_lessons():
    with open(SCHEDULE, encoding="utf-8") as f:
        html = f.read()
    data = json.loads(re.search(r"const DATA = (\{.*?\});\n", html, re.S).group(1))
    return data[GROUP]["lessons"]


def find_subject(lessons, part):
    """(полное название предмета, список преподавателей) по части названия."""
    names = {l["subject"] for l in lessons if part.lower() in l["subject"].lower()}
    return (sorted(names)[0] if names else None)


def explicit_due(text, msg_day):
    """Явная дата сдачи из текста: dd.mm, dd/mm или «d месяца». None, если нет."""
    t = URL.sub(" ", text.lower())
    cands = []
    for d, m in re.findall(r"(?<![\d.])(\d{1,2})[./](\d{1,2})(?![\d.])", t):
        cands.append((int(d), int(m)))
    for d, mon in re.findall(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")", t):
        cands.append((int(d), MONTHS[mon]))
    for d, m in cands:
        for year in (msg_day.year, msg_day.year + 1):
            try:
                dt = date(year, m, d)
            except ValueError:
                continue
            if msg_day <= dt <= msg_day + timedelta(days=60):
                return dt.isoformat()
    return None


def classify(text, lessons, msg_day, has_media=False):
    """Элемент homework.json или None, если сообщение не ДЗ / предмет не определён."""
    clean = URL.sub(" ", text)
    tags = [t.lower() for t in TAG.findall(clean)]
    words = re.findall(r"\w+", clean.lower())
    found = None  # (subject_part, teacher)
    body = TAG.sub(" ", clean).strip()
    for pat, part, teacher, tag_only in ALIASES:
        if any(re.match(pat, t) for t in tags):
            found = (part, teacher, True)
            break
    if found is not None and len(body) < 3 and not has_media:
        return None  # одни хэштеги, самого задания в сообщении нет
    # без хэштега: вопросы («что по волковой задали?») и длинные объявления - не ДЗ
    if found is None and HW_WORDS.search(clean) and "?" not in clean and len(clean) <= 500:
        for pat, part, teacher, tag_only in ALIASES:
            if not tag_only and any(re.match(pat, w) for w in words):
                found = (part, teacher, False)
                break
    if found is None:
        return None
    subject = find_subject(lessons, found[0])
    if not subject:
        return None
    return {"subject": subject, "teacher": found[1], "tagged": found[2],
            "after": msg_day.isoformat(), "due": explicit_due(clean, msg_day)}


async def fetch(api_id, api_hash, workdir, last_id, lessons):
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
            text = m.text or m.caption or ""
            if not text:
                continue
            local = dt.astimezone(MSK)
            item = classify(text, lessons, local.date(), has_media=bool(m.media))
            if item:
                item["id"] = m.id
                item["url"] = "https://t.me/%s/%d" % (CHAT, m.id)
                found.append(item)
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
    lessons = load_lessons()
    state = load_json(STATE, {})
    last_id = int(state.get("last_id", 0))
    with tempfile.TemporaryDirectory() as tmp:
        copy_session(tmp)
        found, max_id = asyncio.run(fetch(api_id, api_hash, tmp, last_id, lessons))

    items = [i for i in load_json(OUT, {}).get("items", []) if "subject" in i]  # старый формат отбрасываем
    known = {i["id"] for i in items}
    added = [f for f in found if f["id"] not in known]
    items = sorted(items + added, key=lambda i: i["id"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"group": GROUP, "items": items}, f, ensure_ascii=False, indent=1)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"last_id": max_id}, f)
    print("новых ДЗ: %d, всего: %d, last_id=%d" % (len(added), len(items), max_id))
    for a in added:
        print("  %s %s -> %s%s%s" % (a["id"], a["after"], a["subject"][:35],
              " (%s)" % a["teacher"] if a["teacher"] else "", " до " + a["due"] if a["due"] else ""))
    if os.environ.get("HW_PUSH") == "1":
        print("push:", "да" if push(OUT) else "изменений нет")


if __name__ == "__main__":
    sys.exit(main())
