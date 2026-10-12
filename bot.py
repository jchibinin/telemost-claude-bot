"""Claude-бот для чатов Яндекс Мессенджера (в т.ч. чатов встреч Телемоста).

Long polling Bot API -> Claude -> sendText обратно в тот же чат.

    python bot.py          # работать
    python bot.py --dump   # только печатать входящие апдейты как есть (для отладки)

Команды в чате: /new - забыть переписку, /clear - удалить переписку из чата и забыть её.
"""

import argparse
import glob
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

API = "https://botapi.messenger.yandex.net/bot/v1"
TOKEN = os.environ["YANDEX_BOT_TOKEN"]
# api - Anthropic API (ANTHROPIC_API_KEY); cli - Claude Code `claude -p` по подписке (CLAUDE_CODE_OAUTH_TOKEN)
BACKEND = os.getenv("BACKEND", "api")
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.getenv("CLAUDE_EFFORT", "low")
# Пусто -> отвечать на каждое сообщение. Иначе только если в тексте есть одно из слов.
TRIGGERS = [t.strip().lower() for t in os.getenv("BOT_TRIGGERS", "").split(",") if t.strip()]
HISTORY = int(os.getenv("BOT_HISTORY", "100"))  # сколько последних сообщений чата видит Claude
STATE = Path(__file__).with_name("state.json")  # offset, чтобы после рестарта не отвечать повторно
HISTORY_FILE = Path(__file__).with_name("history.json")  # переписка по чатам с последней очистки
KEEP = 2000  # сколько сообщений на чат помнить (для /clear), в Claude уходят последние HISTORY
MAX_TEXT = 6000  # лимит sendText
CMD_NEW = {"/new", "/новый"}
CMD_CLEAR = {"/clear", "/очистить"}
ME = "Клод (ты)"

SYSTEM = os.getenv("BOT_SYSTEM") or (
    "Ты ассистент в групповом чате видеовстречи (Яндекс Телемост). "
    "Тебя зовут Клод. Тебе приходит переписка чата в формате «Имя: текст», твои прошлые ответы "
    f"подписаны «{ME}». Ответь на новые сообщения в конце переписки, учитывая весь контекст, "
    "кратко и по делу, на языке собеседника.\n\n"
    "Чат понимает только такую разметку: **жирный**, __курсив__, ~~зачёркнутый~~, ++подчёркнутый++, "
    "`код`, [текст](ссылка) и блоки кода в тройных обратных кавычках с языком. "
    "Курсив только через двойное подчёркивание, *одинарные звёздочки* не работают. "
    "Заголовков (#), таблиц и цитат (>) нет - вместо них жирный текст и простые списки с «- » или «1. »."
)

log = logging.getLogger("bot")
session = requests.Session()
session.headers["Authorization"] = f"OAuth {TOKEN}"
claude = None  # anthropic.Anthropic(), создаётся при первом запросе: импорт anthropic занимает секунды


def bot_call(method: str, payload: dict, timeout: float = 30) -> dict:
    r = session.post(f"{API}/messages/{method}/", json=payload, timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: HTTP {r.status_code} {data.get('description') or data}")
    return data


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# chat key -> {"target": {chat_id | login [, thread_id]}, "messages": [{"id", "name", "text"}]}
chats: dict[str, dict] = load_json(HISTORY_FILE, {})


def chat_of(u: dict) -> dict:
    chat = u.get("chat") or {}
    if chat.get("type") == "private":
        key, target = "login:" + u["from"]["login"], {"login": u["from"]["login"]}
    else:
        key, target = "chat:" + chat["id"], {"chat_id": chat["id"]}
        if u.get("thread_id"):
            target["thread_id"] = u["thread_id"]
    c = chats.setdefault(key, {"messages": []})
    c["target"] = target
    return c


def remember(c: dict, msg_id: int, name: str, text: str) -> None:
    c["messages"].append({"id": msg_id, "name": name, "text": text})
    del c["messages"][:-KEEP]


def wants_reply(u: dict, text: str) -> bool:
    if (u.get("chat") or {}).get("type") == "private" or not TRIGGERS:
        return True
    return any(t in text.lower() for t in TRIGGERS)


def find_cli() -> str:
    """CLAUDE_CLI, `claude` из PATH или самый свежий claude.exe из десктопного приложения."""
    if os.getenv("CLAUDE_CLI"):
        return os.environ["CLAUDE_CLI"]
    if shutil.which("claude"):
        return shutil.which("claude")
    found = glob.glob(os.path.expandvars(r"%APPDATA%\Claude\claude-code\*\*\claude.exe"))
    if not found:
        raise SystemExit("claude CLI не найден - укажите путь в CLAUDE_CLI")
    return max(found, key=os.path.getmtime)


def ask_claude_cli(prompt: str) -> str:
    # --tools "": без инструментов - сообщения из чата не могут заставить его что-то выполнить на этой машине
    cmd = [find_cli(), "-p", "--model", MODEL, "--effort", EFFORT, "--tools", "",
           "--strict-mcp-config", "--no-session-persistence", "--system-prompt", SYSTEM]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # ключ перебил бы подписку
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       timeout=300, env=env, cwd=Path(__file__).parent)
    if r.returncode != 0:
        raise RuntimeError(f"claude -p: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def ask_claude(messages: list[dict]) -> str:
    transcript = "\n".join(f"{m['name']}: {m['text']}" for m in messages[-HISTORY:])
    prompt = f"Переписка в чате:\n\n{transcript}"
    if BACKEND == "cli":
        return ask_claude_cli(prompt)
    global claude
    if claude is None:
        import anthropic
        claude = anthropic.Anthropic()
    resp = claude.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": EFFORT},
        # при отказе классификатора сервер сам переадресует запрос подходящей модели
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        return "Не могу ответить на это сообщение."
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def send(c: dict, text: str, remember_as: str | None = ME) -> None:
    for i in range(0, len(text), MAX_TEXT):
        part = text[i:i + MAX_TEXT]
        msg_id = bot_call("sendText", {**c["target"], "text": part})["message_id"]
        if remember_as:
            remember(c, msg_id, remember_as, part)


def clear(c: dict, cmd_id: int) -> None:
    """Удалить из чата всё, что бот видел с последней очистки (вместе с самой командой)."""
    ids = [m["id"] for m in c["messages"]] + [cmd_id]
    failed = 0
    for msg_id in ids:
        try:
            bot_call("delete", {**c["target"], "message_id": msg_id})
        except (requests.RequestException, RuntimeError, ValueError) as e:
            failed += 1
            log.warning("delete %s: %s", msg_id, e)
    c["messages"] = []
    log.info("cleared %d messages, %d failed", len(ids) - failed, failed)
    if failed:
        # служебное сообщение в контекст не попадает, но удалится следующим /clear
        send(c, f"Контекст очищен. Удалить не удалось: {failed} из {len(ids)} сообщений.", remember_as="")


def take(u: dict) -> dict | None:
    """Записать апдейт в историю; вернуть чат, если на него надо ответить."""
    sender = u.get("from") or {}
    text = (u.get("text") or "").strip()
    if not text or sender.get("robot"):  # не текст или сообщение бота (в т.ч. наше)
        return None
    c = chat_of(u)
    name = sender.get("display_name") or sender.get("login") or "?"
    log.info("[%s] %s: %s", c["target"], name, text)
    cmd = text.lower()
    if cmd in CMD_NEW:
        c["messages"] = []
        send(c, "Начинаю разговор заново, прошлую переписку не помню.", remember_as="")
        return None
    if cmd in CMD_CLEAR:
        clear(c, u["message_id"])
        return None
    remember(c, u["message_id"], name, text)
    return c if wants_reply(u, text) else None


def reply(c: dict) -> None:
    try:
        answer = ask_claude(c["messages"])
    except Exception as e:  # anthropic.APIError, ошибка/таймаут claude -p
        log.exception("Claude error")
        answer = f"Ошибка Claude: {type(e).__name__}"
    if answer:
        send(c, answer)
        log.info("[%s] -> %s", c["target"], answer[:200])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true", help="печатать сырые апдейты и не отвечать")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    me = session.get(f"{API}/self/get", timeout=10)
    log.info("bot: %s", me.text[:300])
    offset = load_json(STATE, {}).get("offset", 0)
    log.info("polling, offset=%d, triggers=%s, backend=%s, model=%s",
             offset, TRIGGERS or "все сообщения", BACKEND, MODEL)
    while True:
        try:
            updates = bot_call("getUpdates", {"offset": offset, "limit": 100})["updates"]
        except (requests.RequestException, RuntimeError, ValueError) as e:
            log.warning("getUpdates: %s", e)
            time.sleep(5)
            continue
        # Всё, что пришло, пока Claude думал над прошлым ответом, - одна пачка: один ответ на чат.
        to_reply: dict[int, dict] = {}
        for u in updates:
            if args.dump:
                print(json.dumps(u, ensure_ascii=False, indent=2))
            else:
                try:
                    c = take(u)
                    if c is not None:
                        to_reply[id(c)] = c
                    elif u.get("text", "").strip().lower() in CMD_NEW | CMD_CLEAR:
                        to_reply.pop(id(chat_of(u)), None)  # очистили - на старое не отвечаем
                except Exception:
                    log.exception("update %s failed", u.get("update_id"))
            offset = max(offset, u["update_id"] + 1)
        if updates:
            save_json(STATE, {"offset": offset})
        for c in to_reply.values():
            reply(c)
        if updates and not args.dump:
            save_json(HISTORY_FILE, chats)
        if not updates:
            time.sleep(1)


if __name__ == "__main__":
    main()
