import pytest

import bot

P = "5213311111111"


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def say(text=None, **kw):
    return bot.handle_message(P, text, **kw)


def test_welcome_and_menu_number():
    assert "Somos Lavê" in say("Hola!")[0]
    assert "Aceptamos efectivo" in say("2")[0]


def test_faq_accents_case_and_typos():
    assert "Precios" in say("CUÁNTO CUESTA lavar?")[0]
    assert "Precios" in say("presios")[0]
    assert "Av. Aviación" in say("donde estan ubicados")[0]


def test_unknown_logs_and_offers_menu():
    r = say("xyzzy plugh")
    assert "no te entendí" in r[0] and "1. Precios" in r[1]
    assert bot.q("select text from unanswered")[0]["text"] == "xyzzy plugh"


def test_pickup_flow_servicio_ubicacion_horario_confirmacion():
    r = say("¿pasan por mi ropa?")
    assert "5️⃣ Nosotros confirmamos la agenda" in r[0] and "servicio" in r[1]
    assert "ubicación" in say("tenis")[0]
    assert "horario" in say(location={"latitude": 20.7, "longitude": -103.4})[0]
    assert "Revisa tu solicitud" in say("3")[0]
    done = say("Sí")[0]
    assert "#1" in done and "confirmamos la agenda" in done  # received, not booked
    o = bot.q("select * from orders")[0]
    assert (o["service"], o["status"]) == ("Limpieza de tenis", "nuevo")
    assert o["time_window"].startswith("Tarde") and "20.7,-103.4" in o["location"]
    assert "Agenda confirmada" in bot.set_status(1, "programado")[0]


def test_menu_5_address_text_free_time_and_cancel():
    assert "Así de fácil" in say("5")[0]
    say("2")
    assert "ubicación" in say("aqui")[0]  # too short to be an address: re-ask
    say("Av. Patria 1234, Zapopan")
    say("a las 5")
    assert "cancelamos" in say("no")[0]
    assert bot.q("select count(*) n from orders")[0]["n"] == 0


def test_invalid_option_reprompts():
    say("pedido")
    r = say("9")
    assert "Elige una" in r[0]
    assert bot.q("select state from users")[0]["state"] == "ask_service"


def test_handoff_silences_bot_until_reset():
    assert "equipo" in say("quiero hablar con una persona")[0]
    assert say("hola") == []
    bot.reset_user(P)
    assert say("hola")


def test_repeated_unknown_hands_off():
    say("asdf qwer")
    assert "equipo" in say("zxcv uiop")[0]
    assert bot.q("select needs_human from users")[0]["needs_human"] == 1


def test_listo_notification_and_review_after_24h():
    for m in ["pedido", "1", "Calle Falsa 123, Zapopan", "1", "si"]:
        say(m)
    msgs = bot.set_status(1, "listo")
    assert "está lista" in msgs[0] and "Av. Aviación" in msgs[0]
    assert bot.send_due_reviews() == 0
    import time
    assert bot.send_due_reviews(now=time.time() + 25 * 3600) == 1
    assert bot.send_due_reviews(now=time.time() + 50 * 3600) == 0  # only once


def test_notifications_use_meta_template_outside_dry_run(monkeypatch):
    for m in ["pedido", "1", "Calle Falsa 123, Zapopan", "1", "si"]:
        say(m)
    monkeypatch.delenv("DRY_RUN")
    posted = []
    monkeypatch.setattr(bot, "_post", lambda phone, payload: posted.append((phone, payload)))
    text = bot.set_status(1, "listo")[0]
    phone, p = posted[0]
    assert phone == P and p["type"] == "template" and p["template"]["name"] == "lave_ropa_lista"
    params = [x["text"] for x in p["template"]["components"][0]["parameters"]]
    assert params[0] == "1" and params[1].startswith("Av. Aviación")
    assert "está lista" in text  # API/admin still get the rendered text


def test_menus_go_out_as_whatsapp_lists_and_taps_drive_the_flow(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    import hashlib, hmac, json
    monkeypatch.delenv("DRY_RUN")
    monkeypatch.setenv("WA_APP_SECRET", "s3cret")
    posted = []
    monkeypatch.setattr(bot, "_post", lambda phone, payload: posted.append(payload))
    tap = lambda i: {"entry": [{"changes": [{"value": {"messages": [  # noqa: E731
        {"from": P, "type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": i}}}]}}]}]}
    def post(payload):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert client.post("/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200

    with TestClient(app.app) as client:
        post({"entry": [{"changes": [{"value": {"messages": [
            {"from": P, "type": "text", "text": {"body": "hola"}}]}}]}]})
        menu = posted[-1]["interactive"]
        assert posted[-1]["type"] == "interactive" and menu["type"] == "list"
        assert [r["id"] for r in menu["action"]["sections"][0]["rows"]] == list("123456")
        assert "1." not in menu["body"]["text"] and "🔒" in menu["body"]["text"]
        post(tap("5"))  # Solicitar recolección
        post(tap("5"))  # Limpieza de tenis
    assert bot.q("select state, draft from users")[0]["state"] == "ask_location"
    assert "tenis" in bot.q("select draft from users")[0]["draft"]


def test_webhook_signature_and_message(monkeypatch):
    import hashlib, hmac, json
    from fastapi.testclient import TestClient
    import app
    monkeypatch.delenv("DRY_RUN")
    monkeypatch.setenv("WA_APP_SECRET", "s3cret")
    sent = []
    monkeypatch.setattr(bot, "send", lambda phone, text: sent.append((phone, text)))
    body = json.dumps({"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": P, "profile": {"name": "Ana"}}],
        "messages": [{"from": P, "type": "text", "text": {"body": "hola"}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    with TestClient(app.app) as client:
        assert client.post("/webhook", content=body, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 403
        assert client.post("/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
    assert sent and "Somos Lavê" in sent[0][1]
    assert bot.q("select name from users")[0]["name"] == "Ana"
