"""HTTP transport: WhatsApp Cloud API webhook, staff/ERP status hook, admin page, review scheduler.
Runs under uvicorn (Docker) or AWS Lambda (`handler` at the bottom, see template.yaml)."""
import asyncio
import hashlib
import hmac
import html
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

if prefix := os.getenv("SSM_PREFIX"):  # Lambda: secrets live in SSM Parameter Store, e.g. /lave-bot/WA_TOKEN
    import boto3
    for page in boto3.client("ssm").get_paginator("get_parameters_by_path").paginate(Path=prefix, WithDecryption=True):
        for p in page["Parameters"]:
            os.environ.setdefault(p["Name"].rsplit("/", 1)[1], p["Value"])

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, PlainTextResponse  # noqa: E402
from mangum import Mangum  # noqa: E402

import bot  # noqa: E402


@asynccontextmanager
async def lifespan(app):
    async def review_loop():
        while True:
            await asyncio.to_thread(bot.send_due_reviews)
            await asyncio.to_thread(bot.purge_messages)
            await asyncio.sleep(60)

    task = asyncio.create_task(review_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def check_admin(request: Request):
    token = os.getenv("ADMIN_TOKEN")
    if not token and os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        raise HTTPException(401)  # public Function URL: never serve /admin without a token
    if token and not hmac.compare_digest(request.headers.get("x-admin-token") or request.query_params.get("token", ""), token):
        raise HTTPException(401)


@app.get("/webhook")
def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN"):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403)


def first_time(msg_id):
    """Meta re-delivers webhooks it thinks failed; claim the id first so a retry is a no-op.
    ponytail: claim-then-handle, a crash mid-handle drops that message rather than doubling it."""
    return not msg_id or bot.db.claim_seen(msg_id)


def process(payload):
    # No global lock: concurrent messages from one customer are serialized by the user's version (bot.retry).
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            names = {ct.get("wa_id"): ct.get("profile", {}).get("name") for ct in v.get("contacts", [])}
            for m in v.get("messages", []):  # status-only payloads (v["statuses"]) have no messages
                if not first_time(m.get("id")):
                    continue
                phone = m["from"]
                replies = bot.handle_message(phone, text=bot.incoming_text(m),
                                             location=m.get("location"), name=names.get(phone))
                for r in replies:
                    bot.send(phone, r)
            # Coexistence: staff replied from the WhatsApp Business app on the same number.
            for m in v.get("message_echoes", []):
                if first_time(m.get("id")):
                    bot.staff_message(m["to"], bot.incoming_text(m) or f"[{m.get('type', 'mensaje')}]", "staff_app")


@app.post("/webhook")
async def receive(request: Request):
    body = await request.body()
    if os.getenv("DRY_RUN") != "1":
        secret = os.getenv("WA_APP_SECRET", "").encode()
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "bad signature")
    # Synchronous so Lambda doesn't freeze mid-reply; if Meta times out and retries, first_time() drops the copy.
    await asyncio.to_thread(process, await request.json())
    return {"ok": True}


@app.post("/orders/{order_id}/status")
async def order_status(order_id: int, request: Request):
    check_admin(request)
    body = await request.json()
    status = body.get("status")
    if status not in bot.STATUSES:
        raise HTTPException(422, f"status must be one of {bot.STATUSES}")
    msgs = bot.set_status(order_id, status, body.get("confirmed_slot"))
    if msgs is None:
        raise HTTPException(404)
    return {"id": order_id, "status": status, "sent": msgs}


@app.post("/users/{phone}/reset")
def user_reset(phone: str, request: Request):
    check_admin(request)
    bot.reset_user(phone)
    return {"ok": True}


@app.post("/users/{phone}/reply")
async def user_reply(phone: str, request: Request):
    """Staff reply from /admin. Free text only works within 24h of the customer's last message (WhatsApp rule)."""
    check_admin(request)
    text = ((await request.json()).get("text") or "").strip()
    if not text:
        raise HTTPException(422, "text required")
    last = bot.last_customer_at(phone)
    if not last or time.time() - last > 24 * 3600:
        raise HTTPException(409, "Fuera de la ventana de 24 h: el cliente tiene que escribir primero.")
    if not bot.send(phone, text):
        raise HTTPException(502, "WhatsApp no aceptó el mensaje; revisa el log.")
    bot.staff_message(phone, text, "staff_admin")
    return {"ok": True}


def e(v):
    return html.escape(str(v or ""))


def chat_link(request, phone, label):
    return f"<a href=\"/admin/chat/{quote(phone)}?token={quote(request.query_params.get('token', ''))}\">{e(label or phone)}</a>"


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    check_admin(request)
    orders = "".join(
        f"<tr><td>{o['id']}</td><td>{chat_link(request, o['phone'], o['name'])}<br><small>{e(o['phone'])}</small></td><td>{e(o['service'])}</td>"
        f"<td>{e(o['location'])}</td><td>{e(o['time_window'])}</td><td><b>{e(o['status'])}</b></td>"
        f"<td><input id=slot{o['id']} value=\"{e(o['confirmed_slot'] or o['time_window'])}\"> "
        f"<button onclick=\"st({o['id']},'programado',document.getElementById('slot{o['id']}').value)\">programado</button> "
        f"<button onclick=\"st({o['id']},'listo')\">listo</button> "
        f"<button onclick=\"st({o['id']},'entregado')\">entregado</button></td></tr>"
        for o in bot.db.orders(200))
    humans = "".join(
        f"<li>{chat_link(request, u['phone'], u['name'])} {e(u['phone'])} <button onclick=\"rs('{e(u['phone'])}')\">reactivar bot</button></li>"
        for u in bot.db.waiting_users()) or "<li>—</li>"
    chats = "".join(
        f"<li>{'🙋 ' if r['needs_human'] else ''}{chat_link(request, r['phone'], r['name'])} <small>{e(r['phone'])}</small>"
        f" — {e(r['text'][:80])}</li>"
        for r in bot.db.recent_chats(30)) or "<li>—</li>"
    unanswered = "".join(f"<li>{e(r['text'])} <small>({e(r['phone'])})</small></li>"
                         for r in bot.db.unanswered(30)) or "<li>—</li>"
    token = e(request.query_params.get("token", ""))
    return f"""<!doctype html><meta charset=utf-8><title>Lavê admin</title>
<style>body{{font-family:system-ui;margin:2rem}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:.4rem}}</style>
<h1>Lavê · solicitudes de recolección</h1>
<table><tr><th>#</th><th>Cliente</th><th>Servicio</th><th>Dirección</th><th>Horario</th><th>Estado</th><th></th></tr>{orders}</table>
<h2>Esperando a una persona</h2><ul>{humans}</ul>
<h2>Conversaciones recientes</h2><ul>{chats}</ul>
<h2>Preguntas sin respuesta (para agregar al YAML)</h2><ul>{unanswered}</ul>
<script>
const H={{'Content-Type':'application/json','X-Admin-Token':'{token}'}};
const st=(id,s,slot)=>fetch(`/orders/${{id}}/status`,{{method:'POST',headers:H,body:JSON.stringify({{status:s,confirmed_slot:slot}})}}).then(()=>location.reload());
const rs=p=>fetch(`/users/${{p}}/reset`,{{method:'POST',headers:H}}).then(()=>location.reload());
</script>"""


WHO_LABEL = {"bot": "🤖 Bot", "staff_app": "🙋 Staff · app de WhatsApp", "staff_admin": "🙋 Staff · admin"}


@app.get("/admin/chat/{phone}", response_class=HTMLResponse)
def admin_chat(phone: str, request: Request):
    check_admin(request)
    u = bot.db.user(phone)
    name = u.get("name")
    msgs = "".join(
        f'<div class="m {r["who"]}"><small>{e(WHO_LABEL.get(r["who"], name or "Cliente"))} · '
        f'{time.strftime("%d/%m %H:%M", time.localtime(r["created_at"]))}</small>{e(r["text"])}</div>'
        for r in bot.db.messages(phone)) or "<p>Sin mensajes.</p>"
    last = bot.last_customer_at(phone)
    window = (f"<p class=warn>⚠️ El cliente escribió hace más de 24 h: WhatsApp no permite texto libre hasta que vuelva"
              f" a escribir.</p>" if not last or time.time() - last > 24 * 3600 else "")
    paused = ("<p>🙋 El bot está en pausa: el staff atiende este chat. "
              "<button onclick=rs()>reactivar bot</button></p>" if u.get("needs_human") else "<p>🤖 El bot está atendiendo.</p>")
    token = e(request.query_params.get("token", ""))
    return f"""<!doctype html><meta charset=utf-8><title>Lavê · {e(name or phone)}</title>
<style>body{{font-family:system-ui;margin:0;background:#efe7dd}}header{{background:#075e54;color:#fff;padding:.8rem 1.2rem}}
header a{{color:#cfe}}main{{max-width:760px;margin:auto;padding:1rem}}.m{{white-space:pre-wrap;padding:.5rem .7rem;margin:.4rem 0;
border-radius:10px;max-width:75%;background:#fff;box-shadow:0 1px 1px #0002}}.m small{{display:block;color:#667;font-size:.75rem;margin-bottom:.2rem}}
.bot{{margin-left:auto;background:#d9fdd3}}.staff_app,.staff_admin{{margin-left:auto;background:#cfe3ff}}
.warn{{background:#fff3cd;padding:.5rem;border-radius:8px}}form{{display:flex;gap:.5rem;position:sticky;bottom:0;padding:.6rem 0;background:#efe7dd}}
textarea{{flex:1;padding:.5rem;border-radius:8px;border:1px solid #bbb;font:inherit}}</style>
<header><a href="/admin?token={token}">← admin</a> · <b>{e(name or "Cliente")}</b> {e(phone)}</header>
<main>{paused}{msgs}{window}
<form onsubmit="send();return false"><textarea id=t rows=2 placeholder="Responder como staff…"></textarea><button>Enviar</button></form>
<p id=err class=warn hidden></p></main>
<script>
const H={{'Content-Type':'application/json','X-Admin-Token':'{token}'}}, P={quote(phone)!r};
async function send(){{const r=await fetch(`/users/${{P}}/reply`,{{method:'POST',headers:H,body:JSON.stringify({{text:t.value}})}});
  if(r.ok) return location.reload(); err.hidden=false; err.textContent=(await r.json()).detail;}}
const rs=()=>fetch(`/users/${{P}}/reset`,{{method:'POST',headers:H}}).then(()=>location.reload());
scrollTo(0,document.body.scrollHeight);
setInterval(()=>{{if(!t.value)location.reload()}},10000);
</script>"""


_http = Mangum(app, lifespan="off")  # no review_loop on Lambda: EventBridge calls us instead


def handler(event, context):
    """Lambda entry: Function URL requests go to FastAPI; the 15-min EventBridge schedule sends due reviews."""
    if event.get("source") == "aws.events":
        return {"reviews": bot.send_due_reviews(), "purged": bot.purge_messages()}
    return _http(event, context)
