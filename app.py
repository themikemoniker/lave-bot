"""HTTP transport: WhatsApp Cloud API webhook, staff/ERP status hook, admin page, review scheduler."""
import asyncio
import hashlib
import hmac
import html
import os
import threading
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

import bot


@asynccontextmanager
async def lifespan(app):
    async def review_loop():
        while True:
            await asyncio.to_thread(bot.send_due_reviews)
            await asyncio.sleep(60)

    task = asyncio.create_task(review_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


def check_admin(request: Request):
    token = os.getenv("ADMIN_TOKEN")
    if token and not hmac.compare_digest(request.headers.get("x-admin-token") or request.query_params.get("token", ""), token):
        raise HTTPException(401)


@app.get("/webhook")
def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == os.getenv("WA_VERIFY_TOKEN"):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403)


LOCK = threading.Lock()  # ponytail: global lock serializes all chats; per-phone locks if volume grows


def process(payload):
    with LOCK:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                v = change.get("value", {})
                names = {ct.get("wa_id"): ct.get("profile", {}).get("name") for ct in v.get("contacts", [])}
                for m in v.get("messages", []):  # status-only payloads (v["statuses"]) have no messages
                    # Meta re-delivers webhooks it thinks failed; claim the id first so a retry is a no-op.
                    # ponytail: claim-then-handle, a crash mid-handle drops that message rather than doubling it.
                    if not bot.q("insert or ignore into seen(id) values(?) returning id", m.get("id")):
                        continue
                    phone = m["from"]
                    replies = bot.handle_message(phone, text=bot.incoming_text(m),
                                                 location=m.get("location"), name=names.get(phone))
                    for r in replies:
                        bot.send(phone, r)


@app.post("/webhook")
async def receive(request: Request, bg: BackgroundTasks):
    body = await request.body()
    if os.getenv("DRY_RUN") != "1":
        secret = os.getenv("WA_APP_SECRET", "").encode()
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "bad signature")
    bg.add_task(process, await request.json())  # reply 200 fast; Meta retries slow webhooks
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


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    check_admin(request)
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    orders = "".join(
        f"<tr><td>{o['id']}</td><td>{e(o['name'])}<br><small>{e(o['phone'])}</small></td><td>{e(o['service'])}</td>"
        f"<td>{e(o['location'])}</td><td>{e(o['time_window'])}</td><td><b>{e(o['status'])}</b></td>"
        f"<td><input id=slot{o['id']} value=\"{e(o['confirmed_slot'] or o['time_window'])}\"> "
        f"<button onclick=\"st({o['id']},'programado',document.getElementById('slot{o['id']}').value)\">programado</button> "
        f"<button onclick=\"st({o['id']},'listo')\">listo</button></td></tr>"
        for o in bot.q("select * from orders order by id desc limit 200"))
    humans = "".join(
        f"<li>{e(u['name'])} {e(u['phone'])} <button onclick=\"rs('{e(u['phone'])}')\">reactivar bot</button></li>"
        for u in bot.q("select * from users where needs_human=1")) or "<li>—</li>"
    unanswered = "".join(f"<li>{e(r['text'])} <small>({e(r['phone'])})</small></li>"
                         for r in bot.q("select * from unanswered order by id desc limit 30")) or "<li>—</li>"
    token = e(request.query_params.get("token", ""))
    return f"""<!doctype html><meta charset=utf-8><title>Lavê admin</title>
<style>body{{font-family:system-ui;margin:2rem}}table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:.4rem}}</style>
<h1>Lavê · solicitudes de recolección</h1>
<table><tr><th>#</th><th>Cliente</th><th>Servicio</th><th>Dirección</th><th>Horario</th><th>Estado</th><th></th></tr>{orders}</table>
<h2>Esperando a una persona</h2><ul>{humans}</ul>
<h2>Preguntas sin respuesta (para agregar al YAML)</h2><ul>{unanswered}</ul>
<script>
const H={{'Content-Type':'application/json','X-Admin-Token':'{token}'}};
const st=(id,s,slot)=>fetch(`/orders/${{id}}/status`,{{method:'POST',headers:H,body:JSON.stringify({{status:s,confirmed_slot:slot}})}}).then(()=>location.reload());
const rs=p=>fetch(`/users/${{p}}/reset`,{{method:'POST',headers:H}}).then(()=>location.reload());
</script>"""
