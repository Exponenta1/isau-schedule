#!/usr/bin/env python3
"""Ежедневный сбор домашних заданий из Telegram-группы 5072.

Запускается на Zomro. Читает группу через копию сессии парсер-аккаунта Shiller
(рабочая сессия не трогается) и находит сообщения с ДЗ:
  1. старосты размечают ДЗ хэштегами (#англ, #спсау, #сп #волкова ...) - главный сигнал;
  2. сообщение без хэштега, но со словами «дз/задание/домашка» и названием предмета
     или фамилией преподавателя в тексте.
Кроме ДЗ ищутся сообщения об отмене/переносе пар преподавателя (events).
Для каждого ДЗ определяется предмет (полное название из расписания), преподаватель,
если он указан, и явная дата сдачи, если она написана в тексте («к 18.11», «до 20 ноября»).
Дату самой пары считает страница расписания (учитывает отмены и переносы).

Фото распознаются через tesseract (rus+eng): одиночное фото, фото с подписью, альбом из
нескольких фото (одна запись на весь альбом, ссылка на первое сообщение). Текст на картинке
дополняет подпись: предмет и слова об отмене берутся из подписи и распознанного текста,
а срок сдачи с картинки принимается только рядом со словами «до/к/срок/сдать».

В публичный homework.json пишутся только предмет, даты и ссылка на сообщение,
текст сообщений не сохраняется.

Переменные окружения (необязательно):
  HW_CHAT, HW_PHONE, HW_DATA_DIR, HW_OUT, HW_STATE, HW_SCHEDULE (путь к index.html)
  HW_DAYS   глубина первого прохода в днях (по умолчанию 14)
  HW_PUSH=1 после записи сделать git add/commit/push в репозитории HW_OUT
"""
import asyncio
import io
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
OCR_LANGS = os.environ.get("HW_OCR_LANGS", "rus+eng")
OCR_MAX_PHOTOS = 10          # с альбома распознаём не больше стольки фото
OCR_MAX_BYTES = 15 * 1024 * 1024
OCR_MAX_CHARS = 1500         # длиннее - страница учебника, а не задание
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


DEADLINE = re.compile(r"(?:до|к|срок\w*|сдат\w*|сдач\w*|deadline)\s*[:\-]?\s*"
                      r"((?:\d{1,2}[./]\d{1,2}(?![\d.]))|(?:\d{1,2}\s+(?:" + "|".join(MONTHS) + r")))",
                      re.IGNORECASE)


def ocr_due(ocr, msg_day):
    """Срок сдачи из текста с картинки: только рядом со словами «до/к/срок/сдать»."""
    for m in DEADLINE.finditer(URL.sub(" ", ocr)):
        due = explicit_due(m.group(1), msg_day)
        if due:
            return due
    return None


def classify(text, lessons, msg_day, has_media=False, ocr=""):
    """Элемент homework.json или None, если сообщение не ДЗ / предмет не определён.
    text - подпись/текст сообщения, ocr - текст, распознанный на фото (может быть пустым)."""
    caption = text
    if ocr:
        text = (text + "\n" + ocr[:OCR_MAX_CHARS]).strip()
    clean = URL.sub(" ", text)
    tags = [t.lower() for t in TAG.findall(clean)]
    words = re.findall(r"\w+", clean.lower())
    found = None  # (subject_part, teacher)
    body = TAG.sub(" ", clean).strip()
    cap_clean = URL.sub(" ", caption)
    for pat, part, teacher, tag_only in ALIASES:
        if any(re.match(pat, t) for t in tags):
            found = (part, teacher, True)
            break
    if found is not None and len(body) < 3 and not has_media:
        return None  # одни хэштеги, самого задания в сообщении нет
    # без хэштега: вопросы («что по волковой задали?») и длинные объявления - не ДЗ
    # ограничения (вопрос, длина) считаем по подписи: у картинки текст длинный и с «?» из-за шума
    if found is None and HW_WORDS.search(clean) and "?" not in cap_clean and len(cap_clean) <= 500:
        for pat, part, teacher, tag_only in ALIASES:
            if not tag_only and any(re.match(pat, w) for w in words):
                found = (part, teacher, False)
                break
    if found is None:
        return None
    if not found[2] and (CANCEL.search(clean) or MOVE.search(clean)):
        return None  # без хэштега: объявление об отмене/переносе пар, а не задание
    subject = find_subject(lessons, found[0])
    if not subject:
        return None
    return {"subject": subject, "teacher": found[1], "tagged": found[2],
            "after": msg_day.isoformat(),
            "due": explicit_due(cap_clean, msg_day) or (ocr_due(ocr, msg_day) if ocr else None)}


# --- отмены и переносы пар из сообщений чата ---------------------------------------------
CANCEL = re.compile(r"не будет|отмен|заболел|болеет|нет (?:пары|пар|занятий)|не придет|не придёт|"
                    r"отпустил|разрешил\w* отмен|не проводит|пары нет", re.IGNORECASE)
MOVE = re.compile(r"перенес|перенос|перенёс|поменя", re.IGNORECASE)
HEDGE = re.compile(r"вроде|кажется|наверн|типа|может|возможно|слышал|говорят|не знаю", re.IGNORECASE)
WEEKDAYS = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4), ("суббот", 5)]


def teacher_stems(lessons):
    """{основа фамилии: фамилия} из расписания: Волкова А.А. -> {'волко': 'Волкова'}"""
    out = {}
    for l in lessons:
        sur = (l.get("teacher") or "").split()[0:1]
        if sur:
            sur = sur[0]
            out[sur.lower()[:max(4, len(sur) - 2)]] = sur
    return out


def event_due(clean, msg_day):
    low = clean.lower()
    due = explicit_due(clean, msg_day)
    if due:
        return due
    if re.search(r"послезавтра", low):
        return (msg_day + timedelta(days=2)).isoformat()
    if re.search(r"завтра", low):
        return (msg_day + timedelta(days=1)).isoformat()
    if re.search(r"сегодня|сейчас", low):
        return msg_day.isoformat()
    for stem, wd in WEEKDAYS:
        if re.search(r"(?<![а-я])" + stem, low):
            delta = (wd - msg_day.weekday()) % 7 or 7
            return (msg_day + timedelta(days=delta)).isoformat()
    return None


def subject_stems(lessons):
    """{полное название предмета: [основы первых двух значимых слов]}"""
    out = {}
    for l in lessons:
        name = l["subject"]
        if name in out:
            continue
        words = [w for w in re.findall(r"\w+", name.lower()) if len(w) >= 4][:2]
        if words:
            out[name] = [w[:5] for w in words]
    return out


def classify_event(text, lessons, msg_day, ocr=""):
    """Отмена/перенос пары по сообщению чата или объявлению на картинке, либо None.
    text - подпись/текст, ocr - текст с картинки."""
    short = text + ("\n" + ocr if ocr and len(ocr) <= 300 else "")
    return event_by_teacher(short, lessons, msg_day) or event_by_subjects(text, ocr, lessons, msg_day)


def event_by_subjects(text, ocr, lessons, msg_day):
    """Объявление кафедры: «занятия в пятницу, 13 ноября отменяются по дисциплинам: ...».
    Преподаватель может быть не из расписания, поэтому ищем названия предметов.
    Нужны слово об отмене/переносе, хотя бы один предмет и явная дата."""
    full = (text + "\n" + ocr[:OCR_MAX_CHARS]).strip()
    clean = URL.sub(" ", full)
    cap = URL.sub(" ", text)
    if "?" in cap or HEDGE.search(cap) or len(cap) > OCR_MAX_CHARS or len(clean) > OCR_MAX_CHARS:
        return None
    is_move = bool(MOVE.search(clean))
    if not is_move and not CANCEL.search(clean):
        return None
    due = explicit_due(clean, msg_day)
    if not due:
        return None  # без даты не знаем, какую пару помечать
    words = re.findall(r"\w+", clean.lower())
    subjects = [name for name, stems in subject_stems(lessons).items()
                if all(any(w.startswith(st) for w in words) for st in stems)]
    if not subjects:
        return None
    return {"kind": "move" if is_move else "cancel", "subjects": sorted(subjects),
            "after": msg_day.isoformat(), "due": due}


def event_by_teacher(text, lessons, msg_day):
    """Отмена/перенос пары преподавателя по короткому сообщению чата или None."""
    clean = URL.sub(" ", text)
    if "?" in clean or len(clean) > 300 or HEDGE.search(clean):
        return None
    is_move = bool(MOVE.search(clean))
    if not is_move and not CANCEL.search(clean):
        return None
    words = re.findall(r"\w+", clean.lower())
    teachers = []
    for stem, sur in teacher_stems(lessons).items():
        if any(w.startswith(stem) for w in words) and sur not in teachers:
            teachers.append(sur)
    if len(teachers) != 1:
        return None  # преподаватель не назван или неоднозначен
    # «6 пару», «3 пара» - номер пары; «3 пары» - это количество, не номер
    pairs = re.findall(r"(?<!\d)([1-6])\s*(?:пару|пара)(?![а-я])", clean.lower())
    ev = {"kind": "move" if is_move else "cancel", "teacher": teachers[0],
          "after": msg_day.isoformat(), "due": event_due(clean, msg_day)}
    if len(pairs) == 1 and not is_move:  # у переноса номер - это новая пара, а не исходная
        ev["pair"] = int(pairs[0])
    return ev


def ocr_image(data):
    """Текст с картинки через tesseract; пустая строка, если не вышло."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "img")
        with open(path, "wb") as f:
            f.write(data)
        try:
            r = subprocess.run(["tesseract", path, "-", "-l", OCR_LANGS], capture_output=True,
                               timeout=90)
        except (OSError, subprocess.TimeoutExpired) as e:
            print("ocr: ошибка %s" % e)
            return ""
    return r.stdout.decode("utf-8", "ignore").strip() if r.returncode == 0 else ""


def is_image(m):
    if m.photo:
        return True
    d = m.document
    return bool(d and (d.mime_type or "").startswith("image/") and (d.file_size or 0) <= OCR_MAX_BYTES)


async def ocr_message(m):
    try:
        buf = await m.download(in_memory=True)
        return ocr_image(bytes(buf.getbuffer()) if isinstance(buf, io.BytesIO) else open(buf, "rb").read())
    except Exception as e:  # битое/недоступное фото не должно ронять весь проход
        print("ocr: сообщение %s: %s" % (m.id, e))
        return ""


def group_messages(msgs):
    """Альбом (одинаковый media_group_id) - одна запись; остальные сообщения - по одному."""
    groups, by_gid = [], {}
    for m in msgs:
        gid = getattr(m, "media_group_id", None)
        if gid:
            if gid not in by_gid:
                by_gid[gid] = []
                groups.append(by_gid[gid])
            by_gid[gid].append(m)
        else:
            groups.append([m])
    return groups


async def fetch(api_id, api_hash, workdir, last_id, lessons):
    found = []
    events = []
    max_id = last_id
    msgs = []
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
            msgs.append(m)
        for group in group_messages(msgs):
            group.sort(key=lambda x: x.id)
            first = group[0]
            dt = first.date.replace(tzinfo=timezone.utc) if first.date.tzinfo is None else first.date
            local = dt.astimezone(MSK)
            # подпись у альбома стоит на одном из сообщений - берём все непустые
            text = "\n".join(t for t in ((x.text or x.caption or "") for x in group) if t)
            ocr = ""
            images = [x for x in group if is_image(x)][:OCR_MAX_PHOTOS]
            if images:
                ocr = "\n".join([t for t in [await ocr_message(x) for x in images] if t])
            if not text and not ocr:
                continue
            has_media = any(x.media for x in group)
            item = classify(text, lessons, local.date(), has_media=has_media, ocr=ocr)
            ev = classify_event(text, lessons, local.date(), ocr=ocr)
            for res in (item, ev):
                if res:
                    res["id"] = first.id
                    res["url"] = "https://t.me/%s/%d" % (CHAT, first.id)
            if item:
                if ocr:
                    item["photo"] = len(images)
                found.append(item)
            if ev:
                events.append(ev)
    return found, events, max_id


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
        found, found_ev, max_id = asyncio.run(fetch(api_id, api_hash, tmp, last_id, lessons))

    old = load_json(OUT, {})
    items = [i for i in old.get("items", []) if "subject" in i]  # старый формат отбрасываем
    events = old.get("events", [])
    ev_known = {e["id"] for e in events}
    added_ev = [e for e in found_ev if e["id"] not in ev_known]
    events = sorted(events + added_ev, key=lambda e: e["id"])
    known = {i["id"] for i in items}
    added = [f for f in found if f["id"] not in known]
    items = sorted(items + added, key=lambda i: i["id"])
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"group": GROUP, "items": items, "events": events}, f, ensure_ascii=False, indent=1)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"last_id": max_id}, f)
    print("новых ДЗ: %d, всего: %d; событий (отмены/переносы): %d новых, last_id=%d"
          % (len(added), len(items), len(added_ev), max_id))
    for e in added_ev:
        print("  событие %s %s: %s %s%s" % (e["id"], e["after"], e["kind"],
              e.get("teacher") or "; ".join(x[:30] for x in e.get("subjects", [])),
              " на " + e["due"] if e["due"] else ""))
    for a in added:
        print("  %s %s -> %s%s%s" % (a["id"], a["after"], a["subject"][:35],
              " (%s)" % a["teacher"] if a["teacher"] else "", " до " + a["due"] if a["due"] else ""))
    if os.environ.get("HW_PUSH") == "1":
        print("push:", "да" if push(OUT) else "изменений нет")


if __name__ == "__main__":
    sys.exit(main())
