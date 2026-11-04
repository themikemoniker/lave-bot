"""Chat with the bot in the terminal. Commands: /loc <lat> <lng> (share location), /reset (staff reset), /quit."""
import os
import sys

os.environ.setdefault("DB_PATH", "sim.db")
os.environ.setdefault("DRY_RUN", "1")
from bot import handle_message, reset_user  # noqa: E402

phone = sys.argv[1] if len(sys.argv) > 1 else "5213300000000"
print(f"Simulando WhatsApp como {phone}. Escribe 'hola'. (/loc 20.70 -103.41, /reset, /quit)\n")
while True:
    try:
        text = input("tú> ").strip()
    except (EOFError, KeyboardInterrupt):
        break
    if text == "/quit":
        break
    if text == "/reset":
        reset_user(phone)
        print("(staff: bot reactivado)\n")
        continue
    if text.startswith("/loc"):
        lat, lng = (text.split()[1:3] + ["20.7015", "-103.4120"])[:2]
        replies = handle_message(phone, location={"latitude": lat, "longitude": lng})
    else:
        replies = handle_message(phone, text, name="Cliente Sim")
    for r in replies or ["(bot en silencio: chat con una persona — /reset para reactivar)"]:
        print(f"lavê> {r}\n")
