#!/usr/bin/env python3
"""GitHub new-repo alert bot -> Telegram.

Ищет свежие репозитории по тегам (topics) из config.json и присылает
новые в Telegram. Только стандартная библиотека Python, ничего ставить не надо.

Переменные окружения:
  GITHUB_TOKEN        токен GitHub (в Actions выдаётся автоматически)
  TELEGRAM_BOT_TOKEN  токен бота от @BotFather
  TELEGRAM_CHAT_ID    id чата, куда слать
  DRY_RUN=1           ничего не отправлять и не сохранять, печатать в консоль
  CONFIG_PATH         другой путь к конфигу (для тестов)
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = ROOT / "seen.json"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SEARCH_PAUSE = 2.5        # секунд между запросами к GitHub (лимит 30/мин)
TG_PAUSE = 1.2            # секунд между сообщениями в Telegram
SEEN_KEEP_DAYS = 10       # сколько хранить уже виденные репо
KEEPALIVE_EVERY_DAYS = 14 # раз в сколько дней обновлять метку в seen.json


def log(msg):
    print(msg, flush=True)


def http_json(url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- GitHub ----------

def search_repos(tag, min_stars, since):
    """Возвращает список репо или None, если запрос не удался."""
    query = f"topic:{tag} stars:>={min_stars} created:>={since}"
    params = urllib.parse.urlencode(
        {"q": query, "sort": "stars", "order": "desc", "per_page": 100}
    )
    url = f"https://api.github.com/search/repositories?{params}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "repo-alert-bot",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    for attempt in range(3):
        try:
            return http_json(url, headers).get("items", [])
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                wait = int(e.headers.get("Retry-After") or 30 * (attempt + 1))
                log(f"[{tag}] лимит GitHub ({e.code}), жду {min(wait, 90)} с")
                time.sleep(min(wait, 90))
                continue
            log(f"[{tag}] ошибка GitHub {e.code}")
            return None
        except Exception as e:
            log(f"[{tag}] сетевая ошибка: {type(e).__name__}")
            time.sleep(3)
    return None


def collect(config, since):
    """Ищет по всем тегам, склеивает дубли по id репо."""
    found = {}
    total = failed = 0
    for cat, spec in config["categories"].items():
        for tag in spec["tags"]:
            total += 1
            items = search_repos(tag, spec["min_stars"], since)
            time.sleep(SEARCH_PAUSE)
            if items is None:
                failed += 1
                continue
            log(f"[{cat}/{tag}] найдено: {len(items)}")
            for repo in items:
                entry = found.setdefault(repo["id"], {"repo": repo, "cats": []})
                if cat not in entry["cats"]:
                    entry["cats"].append(cat)
    return found, total, failed


# ---------- Telegram ----------

def format_message(entry):
    r = entry["repo"]
    cats = " ".join("#" + c.replace("-", "_") for c in entry["cats"])
    desc = (r.get("description") or "без описания").strip()
    if len(desc) > 300:
        desc = desc[:297] + "..."
    lang = r.get("language") or "—"
    created = r["created_at"][:10]
    return (
        f"{html.escape(cats)}\n"
        f'<a href="{html.escape(r["html_url"])}"><b>{html.escape(r["full_name"])}</b></a>\n'
        f"{html.escape(desc)}\n\n"
        f"⭐ {r['stargazers_count']} · 🍴 {r['forks_count']} · {html.escape(lang)} · {created}"
    )


def send_telegram(text):
    if DRY_RUN:
        log("---- (dry run) сообщение ----\n" + text + "\n")
        return True
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    for _ in range(3):
        try:
            http_json(url, headers, payload)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 429:
                try:
                    wait = json.loads(body)["parameters"]["retry_after"]
                except Exception:
                    wait = 5
                time.sleep(min(wait + 1, 60))
                continue
            log(f"Telegram ошибка {e.code}: {body[:200]}")
            return False
        except Exception as e:
            log(f"Telegram сетевая ошибка: {type(e).__name__}")
            time.sleep(3)
    return False


# ---------- Состояние ----------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text("utf-8")), False
    return {"seen": {}, "keepalive": ""}, True


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------- Основная логика ----------

def main():
    if not DRY_RUN and not (TG_TOKEN and TG_CHAT):
        sys.exit("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    config = json.loads(CONFIG_PATH.read_text("utf-8"))
    window = config.get("window_days", 2)
    limit = config.get("max_messages_per_run", 10)

    state, first_run = load_state()
    seen = state["seen"]
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=window)).strftime("%Y-%m-%d")

    found, total, failed = collect(config, since)
    if total and failed == total:
        sys.exit("Все запросы к GitHub провалились, состояние не меняю")

    new = [e for rid, e in found.items() if str(rid) not in seen]
    new.sort(key=lambda e: e["repo"]["created_at"])  # старые первыми
    log(f"Всего найдено: {len(found)}, новых: {len(new)}, ошибок запросов: {failed}")

    if first_run and not DRY_RUN:
        # Первый запуск: запоминаем всё найденное, ничего не шлём
        for e in new:
            seen[str(e["repo"]["id"])] = now.isoformat()
        send_telegram(
            f"Бот запущен. За последние {window} дн. найдено {len(new)} репо, "
            "они помечены как просмотренные. Дальше сюда будут приходить только новые."
        )
    else:
        sent = 0
        for e in new[:limit]:
            if send_telegram(format_message(e)):
                seen[str(e["repo"]["id"])] = now.isoformat()
                sent += 1
            time.sleep(0 if DRY_RUN else TG_PAUSE)
        if len(new) > limit:
            log(f"Лимит {limit} сообщений за запуск, ещё {len(new) - limit} уйдут в следующий раз")
        log(f"Отправлено: {sent}")

    if DRY_RUN:
        return

    # Чистим старые записи
    cutoff = now - timedelta(days=max(SEEN_KEEP_DAYS, window + 3))
    for rid in list(seen):
        try:
            if datetime.fromisoformat(seen[rid]) < cutoff:
                del seen[rid]
        except ValueError:
            del seen[rid]

    # Редкий коммит, чтобы GitHub не отключил расписание из-за неактивности
    last = state.get("keepalive", "")
    try:
        stale = not last or (now - datetime.fromisoformat(last)).days >= KEEPALIVE_EVERY_DAYS
    except ValueError:
        stale = True
    if stale:
        state["keepalive"] = now.isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
