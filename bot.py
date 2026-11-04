"""Lavê WhatsApp bot core: transport-independent handler, order state machine, SQLite, outbound send."""
import json
import os
import re
import sqlite3
import time
import unicodedata
from contextlib import closing
from pathlib import Path

import httpx
import yaml
from rapidfuzz import fuzz, process

ROOT = Path(__file__).parent
STATUSES = ("nuevo", "programado", "listo", "entregado")
SCHEMA = """
create table if not exists users(phone text primary key, name text, state text default 'idle',
  draft text default '{}', needs_human int default 0, unknown_count int default 0);
create table if not exists orders(id integer primary key, phone text, name text, service text,
  location text, time_window text, status text default 'nuevo', created_at real, ready_at real,
  review_sent int default 0);
create table if not exists unanswered(id integer primary key, phone text, text text, created_at real);
"""


def cfg():
    # Re-read every call so owners can edit the YAML without restarting.
    return yaml.safe_load(Path(os.getenv("LAVE_CONFIG", ROOT / "config/lave.yaml")).read_text(encoding="utf-8"))


def q(sql, *args):
    # ponytail: connection per query, fine for a single laundry; pool if traffic grows.
    with closing(sqlite3.connect(os.getenv("DB_PATH", ROOT / "lave.db"))) as c:
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split())


def score(text, kw):
    """Best fuzzy ratio of keyword against same-length word windows of text (typo tolerant, no substring false hits)."""
    kw, words = norm(kw), text.split()
    if not words:
        return 0
    if len(kw) <= 3:  # short words: exact only ("tec", "si", "hi")
        return 100 if kw in words else 0
    k = len(kw.split())
    return max(fuzz.ratio(" ".join(words[i:i + k]), kw) for i in range(max(1, len(words) - k + 1)))


def hits(text, kws, c):
    return any(score(text, k) >= c["match_threshold"] for k in kws)


def fmt(template, c, **extra):
    ctx = {**c["business"], **extra}
    ctx["welcome"] = c["messages"]["welcome"].format_map(ctx)
    return template.format_map(ctx).strip()


def numbered(prompt, options):
    return prompt + "\n" + "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))


def pick(t, options):
    if t.isdigit():
        return options[int(t) - 1] if 1 <= int(t) <= len(options) else None
    m = process.extractOne(t, options, processor=norm, scorer=fuzz.WRatio, score_cutoff=75) if t else None
    return m[0] if m else None


def set_state(phone, state, draft=None):
    q("update users set state=?, draft=?, unknown_count=0 where phone=?", state, json.dumps(draft or {}), phone)


def handoff(phone, c, msg=True):
    q("update users set needs_human=1, state='idle', draft='{}' where phone=?", phone)
    return [fmt(c["messages"]["handoff"], c)] if msg else []


def reset_user(phone):
    q("update users set needs_human=0, unknown_count=0, state='idle', draft='{}' where phone=?", phone)


def handle_message(phone, text=None, location=None, name=None):
    """location: {"latitude", "longitude", "name"?, "address"?}. Returns replies to send (may be empty)."""
    c = cfg()
    q("insert or ignore into users(phone) values(?)", phone)
    if name:
        q("update users set name=? where phone=?", name, phone)
    u = q("select * from users where phone=?", phone)[0]
    if u["needs_human"]:
        return []  # staff owns this chat until reset
    t = norm(text)

    if hits(t, c["human_keywords"], c) or (t == "6" and u["state"] == "idle"):
        return handoff(phone, c)
    if u["state"] != "idle":
        if t in map(norm, c["cancel_keywords"]):
            set_state(phone, "idle")
            return [fmt(c["messages"]["order_cancelled"], c)]
        return order_step(phone, u, t, text, location, c)

    action = c["menu"].get(t)
    if not action:
        best = max(((k, max(score(t, kw) for kw in f["keywords"])) for k, f in c["faq"].items()), key=lambda x: x[1])
        action = best[0] if best[1] >= c["match_threshold"] else None

    if action == "human":
        return handoff(phone, c)
    if action:
        q("update users set unknown_count=0 where phone=?", phone)
        reply = [fmt(c["faq"][action]["reply"], c)]
        if c["faq"][action].get("start_order"):
            set_state(phone, "ask_service")
            reply.append(numbered(fmt(c["order"]["start"], c), c["order"]["services"]))
        if c["faq"][action].get("handoff"):
            handoff(phone, c, msg=False)
        return reply

    # Unknown: log it, try optional LLM, else menu; repeated misses -> human.
    q("insert into unanswered(phone, text, created_at) values(?,?,?)", phone, text or "[ubicación]", time.time())
    from llm import llm_answer  # optional fallback, no-op unless configured
    answer = llm_answer(text, c) if text else None
    if answer:
        return [answer]
    n = u["unknown_count"] + 1
    q("update users set unknown_count=? where phone=?", n, phone)
    if n >= c["unknown_before_handoff"]:
        return handoff(phone, c)
    return [fmt(c["messages"]["unknown"], c), fmt(c["messages"]["welcome"], c)]


def order_step(phone, u, t, raw, location, c):
    o, d, st = c["order"], json.loads(u["draft"]), u["state"]
    invalid = fmt(c["messages"]["invalid_option"], c)

    if st == "ask_service":
        if not (s := pick(t, o["services"])):
            return [invalid, numbered(fmt(o["start"], c), o["services"])]
        set_state(phone, "ask_location", {**d, "service": s})
        return [o["ask_location"]]

    if st == "ask_location":
        if location:
            label = location.get("name") or location.get("address") or ""
            d["location"] = f"{label} https://maps.google.com/?q={location['latitude']},{location['longitude']}".strip()
        elif len(t) >= 8:
            d["location"] = raw.strip()
        else:
            return [o["ask_location"]]
        set_state(phone, "ask_time", d)
        return [numbered(o["ask_time"], o["time_windows"])]

    if st == "ask_time":
        if not t:
            return [numbered(o["ask_time"], o["time_windows"])]
        d["time_window"] = pick(t, o["time_windows"]) or raw.strip()  # free text like "a las 5" is fine
        set_state(phone, "confirm", d)
        return [fmt(o["confirm"], c, **d)]

    if st == "confirm":
        if t in map(norm, c["yes_words"]):
            oid = q("insert into orders(phone, name, service, location, time_window, created_at) "
                    "values(?,?,?,?,?,?) returning id", phone, u["name"], d["service"],
                    d["location"], d["time_window"], time.time())[0]["id"]
            set_state(phone, "idle")
            return [fmt(o["saved"], c, order_id=oid)]
        if t in map(norm, c["no_words"]):
            set_state(phone, "idle")
            return [fmt(c["messages"]["order_cancelled"], c)]
        return [fmt(o["confirm"], c, **d)]

    set_state(phone, "idle")  # unknown state: recover
    return [fmt(c["messages"]["welcome"], c)]


# ---- outbound (status notifications, reviews) ----

def _post(phone, payload):
    try:
        r = httpx.post(f"https://graph.facebook.com/v21.0/{os.environ['WA_PHONE_NUMBER_ID']}/messages",
                       headers={"Authorization": f"Bearer {os.environ['WA_TOKEN']}"}, timeout=10,
                       json={"messaging_product": "whatsapp", "to": phone, **payload})
        r.raise_for_status()
    except Exception as e:  # never crash the webhook/scheduler on a failed send
        print(f"[send error → {phone}] {e}", flush=True)


def send(phone, text):
    if os.getenv("DRY_RUN") == "1":
        print(f"[DRY_RUN → {phone}]\n{text}\n", flush=True)
        return
    _post(phone, {"type": "text", "text": {"body": text}})


def notify(phone, key, c, **ctx):
    """Staff-triggered/scheduled message. May land outside WhatsApp's 24h window, so it goes out as the
    Meta-approved template from `wa_templates` when configured (plain text in DRY_RUN). Returns the text."""
    text = fmt(c["templates"][key], c, **ctx)
    t = (c.get("wa_templates") or {}).get(key)
    if not t or os.getenv("DRY_RUN") == "1":
        send(phone, text)
        return text
    values = {**c["business"], **ctx}
    _post(phone, {"type": "template", "template": {
        "name": t["name"], "language": {"code": t.get("language", "es_MX")},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": str(values[p])} for p in t["params"]]}]}})
    return text


def set_status(order_id, status):
    """ERP/staff hook. Returns the messages sent, or None if the order doesn't exist."""
    c = cfg()
    rows = q("update orders set status=?, ready_at=case when ?='listo' then coalesce(ready_at, ?) else ready_at end "
             "where id=? returning phone", status, status, time.time(), order_id)
    if not rows:
        return None
    return [notify(rows[0]["phone"], status, c, order_id=order_id)] if status in c["templates"] else []


def send_due_reviews(now=None):
    c = cfg()
    cutoff = (now or time.time()) - c["business"]["review_delay_hours"] * 3600
    # ponytail: claim-then-send; a failed send skips that review rather than risking duplicates.
    rows = q("update orders set review_sent=1 where status in ('listo','entregado') and review_sent=0 "
             "and ready_at<=? returning id, phone", cutoff)
    for r in rows:
        notify(r["phone"], "review", c, order_id=r["id"])
    return len(rows)
