"""Build docs/lave-bot-showcase.pdf from REAL bot output (runs the handler against a temp DB).

    .venv/bin/python docs/showcase.py

Restyle by editing THEME below; everything else is layout. Needs Google Chrome (headless print-to-pdf).
"""
import html
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"
CHROME = os.getenv("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

THEME = """
  --brand: #1f4e8c;      /* primary */
  --accent: #29a3d6;     /* highlights */
  --ink: #1d2330;
  --muted: #6b7385;
  --paper: #ffffff;
  --soft: #f2f5fa;
  --chat-bg: #efe7dd; --chat-me: #d9fdd3; --chat-bot: #ffffff;
  --font: -apple-system, "Helvetica Neue", Arial, sans-serif;
"""

tmp = Path(tempfile.mkdtemp())
os.environ.update(DB_PATH=str(tmp / "demo.db"), DRY_RUN="1")
os.environ.pop("LLM_FALLBACK", None)
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

sent = []
bot.send = lambda phone, text: sent.append(text)


def convo(phone, steps, name="Ana"):
    out = []
    for s in steps:
        if isinstance(s, dict):
            out.append(("me", "📍 Ubicación compartida"))
            replies = bot.handle_message(phone, location=s, name=name)
        else:
            out.append(("me", s))
            replies = bot.handle_message(phone, s, name=name)
        out += [("bot", r) for r in replies] or [("note", "🤫 Bot en silencio: una persona del equipo atiende")]
    return out


faq = convo("5213310000001", ["Hola", "presios", "aceptan tarjeta?"])
pickup = convo("5213310000002", ["¿pasan por mi ropa?", "1", {"latitude": 20.7015, "longitude": -103.412,
                                                              "name": "Torre Aviación"}, "2", "sí"], name="Luis")
handoff = convo("5213310000003", ["lavan alfombras persas?", "quiero hablar con una persona", "hola?"], name="Sofía")
convo("5213310000004", ["recolección", "2", "Av. Patria 1234, Zapopan", "1", "si"], name="Carla")

# Post-order lifecycle, as the customer receives it.
lifecycle = []
for label, fn in [("Staff confirma la agenda → programado", lambda: bot.set_status(1, "programado")),
                  ("Staff marca → listo", lambda: bot.set_status(1, "listo")),
                  ("24 h después (automático)", lambda: bot.send_due_reviews(now=time.time() + 25 * 3600))]:
    sent.clear()
    fn()
    lifecycle.append(("note", label))
    lifecycle += [("bot", m) for m in sent]
bot.set_status(2, "programado")

# Admin page screenshot.
from fastapi.testclient import TestClient  # noqa: E402
import app  # noqa: E402

(tmp / "admin.html").write_text(TestClient(app.app).get("/admin").text, encoding="utf-8")
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1100,430",
                f"--screenshot={OUT / 'admin.png'}", (tmp / "admin.html").as_uri()], check=True, capture_output=True)

# Facts pulled from the code/config so the PDF can't drift.
c = bot.cfg()
todos = [t for ln in (ROOT / "config/lave.yaml").read_text().splitlines()
         if "TODO_CONFIRM" in ln and not ln.strip().startswith("#")
         and (t := re.sub(r"^(reply: )?[\"']?|[\"']$", "", re.sub(r"\s+#.*$", "", ln).strip(" -•")).strip())
         and not t.endswith(":")]
todos = [t.replace("\"", "") for t in todos]
# Block-scalar text (e.g. the price list) can't carry inline comments, so it's flagged by a "# TODO_CONFIRM: ..." line.
todos += [ln.split("TODO_CONFIRM:", 1)[1].strip() for ln in (ROOT / "config/lave.yaml").read_text().splitlines()
          if ln.strip().startswith("# TODO_CONFIRM:")]
n_tests = (ROOT / "tests/test_bot.py").read_text().count("def test_")
n_lines = sum(len(p.read_text().splitlines()) for p in ROOT.glob("*.py"))
n_kw = sum(len(f["keywords"]) for f in c["faq"].values())


def bubble(kind, text):
    t = html.escape(text)
    t = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", t)
    t = re.sub(r"(https?://\S+)", r'<span class="link">\1</span>', t)
    return f'<div class="msg {kind}">{t}</div>'


def phone(title, msgs):
    return (f'<div class="phone"><div class="bar"><span class="avatar">L</span><div><b>Lavê</b>'
            f'<small>{html.escape(title)}</small></div></div><div class="chat">'
            + "".join(bubble(k, m) for k, m in msgs) + "</div></div>")


steps = [("1", "Escribe por WhatsApp", "Menú de bienvenida o palabra clave «recolección»", "idle"),
         ("2", "Indica el servicio", "Número o texto libre: «tenis», «tintorería»", "ask_service"),
         ("3", "Comparte ubicación", "Ubicación de WhatsApp o dirección escrita", "ask_location"),
         ("4", "Elige horario", "Ventanas desde config (o texto libre)", "ask_time"),
         ("5", "Confirmamos la agenda", "Resumen → «sí» → solicitud <b>nuevo</b>; staff → <b>programado</b>",
          "confirm")]
flow = "".join(f'<div class="step"><div class="num">{n}</div><b>{t}</b><p>{d}</p><code>{s}</code></div>'
               for n, t, d, s in steps)

page = f"""<!doctype html><html lang="es"><meta charset="utf-8"><title>Lavê WhatsApp bot</title>
<style>
:root {{{THEME}}}
@page {{ size: Letter; margin: 0.55in; }}
* {{ box-sizing: border-box; }}
body {{ font-family: var(--font); color: var(--ink); margin: 0; font-size: 10.5pt; line-height: 1.45; }}
section {{ break-before: page; }}
section:first-child {{ break-before: auto; }}
h1 {{ font-size: 28pt; margin: 0 0 4px; color: var(--brand); letter-spacing: -0.5px; }}
h2 {{ font-size: 18pt; color: var(--brand); margin: 0 0 4px; }}
.lead {{ color: var(--muted); margin: 0 0 18px; }}
.hero {{ background: linear-gradient(135deg, var(--brand), var(--accent)); color: #fff; border-radius: 18px;
        padding: 22px 26px; margin-bottom: 14px; }}
.hero h1 {{ color: #fff; }} .hero p {{ font-size: 13pt; margin: 6px 0 0; opacity: .95; }}
.stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }}
.stat {{ background: var(--soft); border-radius: 12px; padding: 14px; }}
.stat b {{ display: block; font-size: 22pt; color: var(--brand); }} .stat span {{ color: var(--muted); font-size: 9.5pt; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
.card {{ border: 1px solid #e3e7ef; border-radius: 12px; padding: 14px 16px; }}
.card h3 {{ margin: 0 0 6px; font-size: 12pt; color: var(--brand); }}
.card ul {{ margin: 0; padding-left: 18px; }}
.flow {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 10px 0 12px; }}
.step {{ background: var(--soft); border-radius: 12px; padding: 12px; position: relative; font-size: 9.5pt; }}
.step:not(:last-child)::after {{ content: "→"; position: absolute; right: -11px; top: 18px; color: var(--accent);
        font-weight: bold; }}
.step p {{ margin: 4px 0 6px; color: var(--muted); }}
.num {{ width: 26px; height: 26px; border-radius: 50%; background: var(--brand); color: #fff; display: grid;
        place-items: center; font-weight: bold; margin-bottom: 6px; }}
code {{ font-size: 8.5pt; background: #fff; border-radius: 4px; padding: 1px 5px; color: var(--brand); }}
.life {{ display: flex; align-items: center; gap: 8px; margin: 8px 0 18px; flex-wrap: wrap; }}
.pill {{ border-radius: 999px; padding: 6px 14px; background: var(--soft); font-weight: 600; font-size: 10pt; }}
.pill.on {{ background: var(--brand); color: #fff; }} .arrow {{ color: var(--accent); font-weight: bold; }}
.phones {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; align-items: start; }}
.phone {{ border-radius: 18px; overflow: hidden; border: 1px solid #d8d2c8; break-inside: avoid; }}
.bar {{ background: #075e54; color: #fff; padding: 8px 12px; display: flex; gap: 10px; align-items: center; }}
.bar small {{ display: block; opacity: .8; font-size: 8.5pt; }}
.avatar {{ width: 28px; height: 28px; border-radius: 50%; background: var(--accent); display: grid; place-items: center;
          font-weight: bold; }}
.chat {{ background: var(--chat-bg); padding: 10px; display: flex; flex-direction: column; gap: 6px; }}
.msg {{ white-space: pre-wrap; max-width: 88%; padding: 6px 9px; border-radius: 9px; font-size: 8.6pt;
       line-height: 1.35; box-shadow: 0 1px 0 rgba(0,0,0,.08); }}
.msg.me {{ background: var(--chat-me); align-self: flex-end; }}
.msg.bot {{ background: var(--chat-bot); align-self: flex-start; }}
.msg.note {{ align-self: center; background: #fff7d6; color: #6b5b00; font-size: 8pt; border-radius: 6px; }}
.link {{ color: #027eb5; word-break: break-all; }}
.shot {{ width: 100%; border: 1px solid #e3e7ef; border-radius: 10px; }}
.arch {{ display: grid; grid-template-columns: 1fr auto 1.3fr auto 1fr; gap: 8px; align-items: center; margin: 14px 0; }}
.box {{ background: var(--soft); border-radius: 10px; padding: 10px 12px; font-size: 9.5pt; }}
.box b {{ color: var(--brand); display: block; }}
.todo {{ columns: 2; font-size: 9pt; margin: 0; padding-left: 18px; }} .todo li {{ break-inside: avoid; }}
</style>

<section>
  <div class="hero"><h1>Lavê · WhatsApp bot</h1>
    <p>Recolecciones, preguntas frecuentes y avisos de «listo» por WhatsApp, sin pagar por herramientas.<br>
    Las reglas y el contenido viven en un solo archivo YAML; la IA es opcional.</p></div>
  <div class="stats">
    <div class="stat"><b>$0</b><span>herramientas: Python, SQLite, WhatsApp Cloud API</span></div>
    <div class="stat"><b>{len(c['faq'])}</b><span>respuestas automáticas · {n_kw} palabras clave</span></div>
    <div class="stat"><b>5</b><span>pasos del proceso oficial de recolección</span></div>
    <div class="stat"><b>{n_tests}</b><span>pruebas automáticas · ~{n_lines} líneas de código</span></div>
  </div>
  <div class="cols">
    <div class="card"><h3>Hoy (manual)</h3><ul>
      <li>El cliente escribe; el staff contesta con respuestas rápidas</li>
      <li>Pedir ubicación y horario, uno por uno</li>
      <li>Capturar en el ERP, avisar «listo», pedir reseña</li></ul></div>
    <div class="card"><h3>Con el bot</h3><ul>
      <li>Menú y FAQ al instante, con errores de dedo y sin acentos</li>
      <li>Recolección guiada: servicio → ubicación → horario</li>
      <li>«Listo» y reseña de Google se envían solos</li>
      <li>Pasa a una persona cuando hace falta</li></ul></div>
  </div>
  <h2 style="margin-top:14px">El proceso oficial, paso a paso</h2>
  <p class="lead">«¿Quieres solicitar una recolección? Solo sigue estos pasos… Así de fácil.» Cada paso es un estado del bot.</p>
  <div class="flow">{flow}</div>
  <h2>Ciclo de la solicitud</h2>
  <div class="life"><span class="pill on">nuevo</span><span class="arrow">→ staff confirma →</span>
    <span class="pill on">programado</span><span class="arrow">→</span><span class="pill on">listo</span>
    <span class="arrow">→ 24 h →</span><span class="pill">reseña de Google ⭐</span></div>
  <p class="lead">El bot nunca promete el horario: la solicitud queda en <b>nuevo</b> hasta que el staff confirma la agenda.</p>
</section>

<section>
  <h2>Conversaciones reales del bot</h2>
  <p class="lead">Generadas al ejecutar el bot. Si cambias el YAML y regeneras este PDF, los textos se actualizan.</p>
  <div class="phones">
    {phone("Solicitar recolección", pickup)}
    {phone("Después de la solicitud", lifecycle)}
  </div>
</section>

<section>
  <div class="phones">
    {phone("Bienvenida y preguntas frecuentes", faq)}
    {phone("Pregunta desconocida y hablar con una persona", handoff)}
  </div>
</section>

<section>
  <h2>Panel del staff</h2>
  <p class="lead">Las solicitudes, los botones <b>programado</b>/<b>listo</b>, los chats que esperan a una persona y las
  preguntas sin respuesta (para agregarlas al YAML). El ERP puede usar el mismo endpoint: <code>POST /orders/{{id}}/status</code>.</p>
  <img class="shot" src="admin.png">
  <h2 style="margin-top:20px">Cómo funciona</h2>
  <div class="arch">
    <div class="box"><b>WhatsApp</b>Cloud API webhook (firma verificada) o simulador en terminal</div><span class="arrow">→</span>
    <div class="box"><b>handle_message()</b>Menú · FAQ con búsqueda aproximada · máquina de estados · pasar a una persona ·
      IA opcional que responde solo con el YAML</div><span class="arrow">→</span>
    <div class="box"><b>SQLite</b>clientes, solicitudes, preguntas sin respuesta</div>
  </div>
  <div class="cols">
    <div class="card"><h3>Todo el contenido en <code>config/lave.yaml</code></h3>
      Horarios, dirección, precios, pagos, palabras clave, plantillas y la liga de reseña. El dueño edita textos sin
      programar; los cambios aplican de inmediato.</div>
    <div class="card"><h3>Privacidad</h3>
      Solo guarda teléfono, nombre, ubicación y pedido. El mensaje de bienvenida lo dice. Modo <code>DRY_RUN</code>
      para revisar mensajes antes de enviarlos.</div>
  </div>
</section>

<section>
  <h2>Pendiente de confirmar con el dueño</h2>
  <p class="lead">{len(todos)} datos marcados <code>TODO_CONFIRM</code> en el YAML. Esta lista se genera del archivo.</p>
  <ul class="todo">{"".join(f"<li>{html.escape(t)}</li>" for t in todos)}</ul>
  <h2 style="margin-top:22px">Siguientes pasos</h2>
  <div class="cols">
    <div class="card"><ul>
      <li>Confirmar con el dueño cada dato TODO_CONFIRM</li>
      <li>Registrar en Meta las plantillas programado / listo / reseña (regla de 24 h)</li>
      <li>Publicar con Docker + cloudflared y conectar el número de WhatsApp</li></ul></div>
    <div class="card"><ul>
      <li>Conectar el ERP al endpoint de estado</li>
      <li>Revisar cada semana las preguntas sin respuesta y ampliar el YAML</li></ul></div>
  </div>
</section>
</html>"""

(OUT / "showcase.html").write_text(page, encoding="utf-8")
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                f"--print-to-pdf={OUT / 'lave-bot-showcase.pdf'}", (OUT / "showcase.html").as_uri()],
               check=True, capture_output=True)
print(OUT / "lave-bot-showcase.pdf")
