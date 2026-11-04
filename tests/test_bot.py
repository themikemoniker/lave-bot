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
    assert "no te entendí" in r[0] and "1️⃣ Precios" in r[1]
    assert bot.q("select text from unanswered")[0]["text"] == "xyzzy plugh"


def test_order_happy_path_pickup_with_location():
    say("quiero hacer un pedido")
    say("tenis")
    assert "ubicación" in say("1")[0]
    say(location={"latitude": 20.7, "longitude": -103.4})
    assert "Confirma" in say("tarde")[0]
    assert "#1" in say("Sí")[0]
    o = bot.q("select * from orders")[0]
    assert (o["service"], o["status"], o["time_window"]) == ("Limpieza de tenis", "nuevo", "Tarde (15–19)")
    assert "20.7,-103.4" in o["location"]


def test_order_dropoff_skips_location_and_cancel():
    say("5")
    say("2")
    assert "horario" in say("sucursal")[0]
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
    for m in ["pedido", "1", "2", "1", "si"]:
        say(m)
    msgs = bot.set_status(1, "listo")
    assert "está listo" in msgs[0] and "Av. Aviación" in msgs[0]
    assert bot.send_due_reviews() == 0
    import time
    assert bot.send_due_reviews(now=time.time() + 25 * 3600) == 1
    assert bot.send_due_reviews(now=time.time() + 50 * 3600) == 0  # only once
