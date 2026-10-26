import os, json, time, hashlib, html, re
from urllib.parse import urlparse
import requests, feedparser

TOKEN = os.environ["TG_BOT_TOKEN"]
ADMIN = int(os.environ["TG_ADMIN_ID"])
CHANNEL = os.environ["TG_CHANNEL"]
GKEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_PER_DAY = int(os.environ.get("MAX_PER_DAY", "4"))
UA = {"User-Agent": "Mozilla/5.0 (recipe-bot)"}

try:
    state = json.load(open("state.json", encoding="utf-8"))
except Exception:
    state = {}
for k, v in {"offset": 0, "seen": [], "drafts": {}, "published": [],
             "edit": None, "day": "", "count": 0}.items():
    state.setdefault(k, v)

def tg(method, **data):
    try:
        return requests.post(f"{API}/{method}", json=data, timeout=30).json()
    except Exception as e:
        print("tg error:", type(e).__name__)
        return {"ok": False}

def hid(s):
    return hashlib.sha1(s.encode()).hexdigest()[:8]

def domain(u):
    return urlparse(u).netloc.replace("www.", "")

def clean(t):
    return re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", html.unescape(str(t)))).strip()

def gemini(prompt):
    if not GKEY:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
        r = requests.post(url, params={"key": GKEY}, timeout=90,
                          json={"contents": [{"parts": [{"text": prompt}]}]})
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print("gemini error:", type(e).__name__)
        return None

def flat(d):
    if isinstance(d, list):
        for x in d:
            yield from flat(x)
    elif isinstance(d, dict):
        yield d
        if "@graph" in d:
            yield from flat(d["@graph"])

def get_recipe(url):
    try:
        page = requests.get(url, headers=UA, timeout=30).text
    except Exception as e:
        print("page error:", type(e).__name__)
        return None
    for m in re.finditer(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for n in flat(data):
            t = n.get("@type")
            if t == "Recipe" or (isinstance(t, list) and "Recipe" in t):
                return n
    return None

def steps(x):
    out = []
    if isinstance(x, str):
        return [x]
    if isinstance(x, list):
        for i in x:
            out += steps(i)
    elif isinstance(x, dict):
        if "itemListElement" in x:
            out += steps(x["itemListElement"])
        elif x.get("text"):
            out.append(x["text"])
    return out

def make_post(n):
    ing = [clean(i) for i in n.get("recipeIngredient", [])]
    st = [clean(s) for s in steps(n.get("recipeInstructions", []))]
    facts = (f"Название: {clean(n.get('name', ''))}\n"
             f"Порции: {clean(n.get('recipeYield', ''))}\n"
             f"Время: {clean(n.get('totalTime', ''))}\n"
             "Ингредиенты:\n- " + "\n- ".join(ing) +
             "\nШаги:\n- " + "\n- ".join(st))
    prompt = (
        "Перепиши этот рецепт для кулинарного Telegram-канала на русском языке СВОИМИ словами. "
        "Все количества переведи в граммы и миллилитры (чашки, унции, фунты, ложки пересчитывай; "
        "для яиц, зубчиков чеснока и т.п. укажи штуки и примерную массу в граммах). "
        "Используй ТОЛЬКО данные ниже, ничего не добавляй и не выдумывай. "
        "Формат: цепляющий заголовок в <b></b>, 1-2 предложения о блюде, "
        "время и порции, блок <b>Ингредиенты</b> списком через «•» с граммовками, "
        "блок <b>Приготовление</b> с нумерованными шагами. До 2500 знаков. "
        "Разрешён только тег <b>, никакой другой разметки.\n\nДанные:\n" + facts)
    t = gemini(prompt)
    if not t:
        return None
    t = html.escape(t).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    return t

def build(d):
    src = ", ".join(f'<a href="{html.escape(u)}">{html.escape(dm)}</a>'
                    for dm, u in d["links"].items())
    return d["text"] + "\n\n📎 Источник: " + src

def card(i):
    d = state["drafts"][i]
    kb = {"inline_keyboard": [[
        {"text": "✅ Опубликовать", "callback_data": f"p:{i}"},
        {"text": "✏️ Изменить", "callback_data": f"e:{i}"}],
        [{"text": "❌ Отклонить", "callback_data": f"r:{i}"},
         {"text": "⏰ Отложить", "callback_data": f"s:{i}"}]]}
    r = tg("sendMessage", chat_id=ADMIN, text=build(d), parse_mode="HTML",
           reply_markup=kb)
    if r.get("ok"):
        d["card"] = r["result"]["message_id"]
    else:
        print("card error:", r.get("description"))

def publish(i):
    d = state["drafts"][i]
    fp = hid(d["title"].lower())
    if d["status"] == "published" or fp in [p["fp"] for p in state["published"]]:
        d["status"] = "published"
        return "Уже публиковалось"
    r = tg("sendMessage", chat_id=CHANNEL, text=build(d), parse_mode="HTML")
    if not r.get("ok"):
        return "Ошибка: " + str(r.get("description", "?"))[:100]
    d["status"] = "published"
    state["published"].append({"id": i, "fp": fp, "title": d["title"],
                               "time": time.strftime("%Y-%m-%d %H:%M"),
                               "links": d["links"]})
    state["published"] = state["published"][-500:]
    return "Опубликовано"

def close_buttons(d):
    if d.get("card"):
        tg("editMessageReplyMarkup", chat_id=ADMIN, message_id=d["card"],
           reply_markup={"inline_keyboard": []})

def on_cb(cb):
    act, i = cb["data"].split(":")
    d = state["drafts"].get(i)
    if not d:
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Не найдено")
        return
    msg = ""
    if act == "p":
        msg = publish(i); close_buttons(d)
    elif act == "r":
        d["status"] = "rejected"; msg = "Отклонено"; close_buttons(d)
    elif act == "s":
        d["status"] = "snoozed"; d["until"] = time.time() + 3 * 3600
        msg = "Отложено на 3 часа"; close_buttons(d)
    elif act == "e":
        state["edit"] = i
        msg = "Пришлите новый текст"
        tg("sendMessage", chat_id=ADMIN, text="Пришлите новый текст поста (ссылка добавится сама).")
    tg("answerCallbackQuery", callback_query_id=cb["id"], text=msg)

def on_msg(m):
    t = m.get("text", "")
    i = state.get("edit")
    if i and t and not t.startswith("/") and i in state["drafts"]:
        d = state["drafts"][i]
        d["text"] = html.escape(t); d["status"] = "pending"
        state["edit"] = None
        close_buttons(d); card(i)

def handle_updates():
    r = tg("getUpdates", offset=state["offset"], timeout=0,
           allowed_updates=["message", "callback_query"])
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        if cb and cb["from"]["id"] == ADMIN:
            on_cb(cb)
        m = u.get("message")
        if m and m.get("from", {}).get("id") == ADMIN:
            on_msg(m)

def wake_snoozed():
    now = time.time()
    for i, d in list(state["drafts"].items()):
        if d["status"] == "snoozed" and d.get("until", 0) <= now:
            d["status"] = "pending"; card(i)
        if now - d["ts"] > 7 * 86400:
            del state["drafts"][i]

def collect():
    if not GKEY:
        print("Нужен GEMINI_API_KEY для перевода в граммы")
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if state["day"] != today:
        state["day"], state["count"] = today, 0
    for line in open("sources.txt", encoding="utf-8"):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if state["count"] >= MAX_PER_DAY:
            return
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print("feed error:", type(e).__name__); continue
        for e in feed.entries[:8]:
            if state["count"] >= MAX_PER_DAY:
                return
            link = e.get("link", "")
            if not link or hid(link) in state["seen"]:
                continue
            ts = time.mktime(e.published_parsed) if e.get("published_parsed") else now
            if now - ts > 14 * 86400:
                state["seen"].append(hid(link)); continue
            n = get_recipe(link)
            if not n or not n.get("recipeIngredient"):
                state["seen"].append(hid(link)); continue
            text = make_post(n)
            if not text:
                print("пропуск: ИИ не ответил, попробую в следующий раз")
                return
            state["seen"].append(hid(link))
            i = hid(link)
            title = clean(n.get("name") or e.get("title", ""))
            state["drafts"][i] = {"title": title, "links": {domain(link): link},
                                  "text": text, "status": "pending", "ts": now}
            state["count"] += 1
            card(i)
    state["seen"] = state["seen"][-3000:]

try:
    handle_updates()
    wake_snoozed()
    if os.environ.get("PAUSED") == "1":
        print("paused")
    else:
        collect()
except Exception as e:
    print("fatal:", type(e).__name__, e)
finally:
    json.dump(state, open("state.json", "w", encoding="utf-8"), ensure_ascii=False)
