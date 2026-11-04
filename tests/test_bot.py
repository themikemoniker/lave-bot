from pathlib import Path

import pytest
import yaml

import bot
import store

P = "5213311111111"


def create_tables_from_template(prefix):
    """Build the DynamoDB tables exactly as template.yaml declares them, so the tests also check the template."""
    import boto3
    loader = type("CfnLoader", (yaml.SafeLoader,), {})
    loader.add_multi_constructor("!", lambda ld, suffix, node: None)  # !Sub, !Ref, ... are irrelevant here
    template = yaml.load((Path(__file__).parent.parent / "template.yaml").read_text(), Loader=loader)
    ddb = boto3.client("dynamodb")
    for name, r in template["Resources"].items():
        if r["Type"] == "AWS::DynamoDB::Table":
            props = {k: v for k, v in r["Properties"].items() if k not in ("TableName", "TimeToLiveSpecification")}
            ddb.create_table(TableName=prefix + name.removesuffix("Table").lower(), **props)


@pytest.fixture(autouse=True, params=["sqlite", "dynamo"])
def tmp_db(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if request.param == "sqlite":
        monkeypatch.setattr(bot, "db", store.SqliteStore())
        yield
        return
    from moto import mock_aws
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("TABLE_PREFIX", "t-")
    with mock_aws():
        create_tables_from_template("t-")
        monkeypatch.setattr(bot, "db", store.DynamoStore())
        yield


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
    assert bot.db.unanswered()[0]["text"] == "xyzzy plugh"


def test_pickup_flow_servicio_ubicacion_horario_confirmacion():
    r = say("¿pasan por mi ropa?")
    assert "5️⃣ Nosotros confirmamos la agenda" in r[0] and "servicio" in r[1]
    assert "ubicación" in say("tenis")[0]
    assert "horario" in say(location={"latitude": 20.7, "longitude": -103.4})[0]
    assert "Revisa tu solicitud" in say("3")[0]
    done = say("Sí")[0]
    assert "#1" in done and "confirmamos la agenda" in done  # received, not booked
    o = bot.db.orders()[0]
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
    assert bot.db.orders() == []


def test_invalid_option_reprompts():
    say("pedido")
    r = say("9")
    assert "Elige una" in r[0]
    assert bot.db.user(P)["state"] == "ask_service"


def test_handoff_silences_bot_until_reset():
    assert "equipo" in say("quiero hablar con una persona")[0]
    assert say("hola") == []
    bot.reset_user(P)
    assert say("hola")


def test_repeated_unknown_hands_off():
    say("asdf qwer")
    assert "equipo" in say("zxcv uiop")[0]
    assert bot.db.user(P)["needs_human"] == 1


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
    assert bot.db.user(P)["state"] == "ask_location"
    assert "tenis" in bot.db.user(P)["draft"]


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
    assert bot.db.user(P)["name"] == "Ana"


def test_webhook_retry_is_deduped_and_statuses_ignored(monkeypatch):
    from fastapi.testclient import TestClient
    import app
    sent = []
    monkeypatch.setattr(bot, "send", lambda phone, text: sent.append((phone, text)))
    msg = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.1", "from": P, "type": "text", "text": {"body": "hola"}}]}}]}]}
    status = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.out", "status": "read"}]}}]}]}
    with TestClient(app.app) as client:
        client.post("/webhook", json=msg)
        n = len(sent)
        client.post("/webhook", json=msg)  # Meta retry
        client.post("/webhook", json=status)
    assert n and len(sent) == n


def test_old_db_gains_confirmed_slot(tmp_path, monkeypatch):
    import sqlite3
    if not isinstance(bot.db, store.SqliteStore):
        pytest.skip("SQLite migration")
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("create table orders(id integer primary key, phone text, name text, service text, location text, "
                  "time_window text, status text default 'nuevo', created_at real, ready_at real, review_sent int default 0)")
        c.execute("insert into orders(phone, time_window) values('521', 'Tarde')")
    monkeypatch.setenv("DB_PATH", str(db))
    assert "Tarde" in bot.set_status(1, "programado")[0]


def test_programado_confirms_exact_slot():
    from fastapi.testclient import TestClient
    import app
    for m in ["pedido", "1", "Calle Falsa 123, Zapopan", "1", "si"]:
        say(m)
    assert "Mañana 9:00" in bot.set_status(1, "programado")[0]  # no slot given: customer's window
    with TestClient(app.app) as client:
        assert 'value="Mañana 9:00' in client.get("/admin").text
        r = client.post("/orders/1/status", json={"status": "programado", "confirmed_slot": "martes 10:30"})
    assert "martes 10:30" in r.json()["sent"][0]
    assert bot.db.orders()[0]["confirmed_slot"] == "martes 10:30"


def test_entregado_thanks_customer_and_still_triggers_review():
    from fastapi.testclient import TestClient
    import time
    import app
    for m in ["pedido", "1", "Calle Falsa 123, Zapopan", "1", "si"]:
        say(m)
    with TestClient(app.app) as client:
        assert "'entregado'" in client.get("/admin").text
        r = client.post("/orders/1/status", json={"status": "entregado"})  # staff skipped listo
    assert "#1 entregada" in r.json()["sent"][0]
    assert bot.send_due_reviews(now=time.time() + 25 * 3600) == 1


def test_conversation_is_logged_for_admin():
    say("hola")
    say(location={"latitude": 20.7, "longitude": -103.4, "name": "Casa"})
    rows = [(r["who"], r["text"]) for r in bot.db.messages(P)]
    assert rows[0] == ("customer", "hola") and rows[1][0] == "bot" and "Lavê" in rows[1][1]
    assert rows[2] == ("customer", "📍 Casa https://maps.google.com/?q=20.7,-103.4")


def test_business_app_echo_is_logged_and_pauses_bot():
    from fastapi.testclient import TestClient
    import app
    say("hola")
    echo = {"entry": [{"changes": [{"field": "smb_message_echoes", "value": {"message_echoes": [
        {"from": "5213300000000", "to": P, "id": "wamid.E1", "type": "text", "text": {"body": "Hola, soy Ana de Lavê"}}]}}]}]}
    with TestClient(app.app) as client:
        client.post("/webhook", json=echo)
        client.post("/webhook", json=echo)  # Meta retry
        assert "Hola, soy Ana de Lavê" in client.get(f"/admin/chat/{P}").text
    staff = [m for m in bot.db.messages(P) if m["who"] == "staff_app"]
    assert [r["text"] for r in staff] == ["Hola, soy Ana de Lavê"]
    assert say("gracias") == []  # staff owns the chat now
    assert bot.db.messages(P)[-1]["text"] == "gracias"


def test_admin_reply_sends_logs_and_respects_24h(monkeypatch):
    from fastapi.testclient import TestClient
    import time
    import app
    say("hola")
    with TestClient(app.app) as client:
        assert client.post(f"/users/{P}/reply", json={"text": "Te escribo de Lavê"}).status_code == 200
        assert "🙋 Staff · admin" in client.get(f"/admin/chat/{P}").text
        assert client.post("/users/5210000000000/reply", json={"text": "hola"}).status_code == 409  # never wrote
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 25 * 3600)
        assert client.post(f"/users/{P}/reply", json={"text": "¿sigues ahí?"}).status_code == 409
    assert bot.db.user(P)["needs_human"] == 1
    assert sum(m["who"] == "staff_admin" for m in bot.db.messages(P)) == 1


def test_purge_drops_only_old_messages():
    import time
    say("hola")
    assert bot.purge_messages() == 0
    assert bot.purge_messages(now=time.time() + 91 * 86400) == 2  # customer + bot


def test_stale_user_write_conflicts_and_turn_is_retried(monkeypatch):
    a, b = bot.db.user(P), bot.db.user(P)
    bot.save(a, state="ask_service")
    with pytest.raises(store.Conflict):
        bot.save(b, state="confirm")  # b read version 0, a already bumped it
    real, calls = bot.db.save_user, []
    def flaky(u):  # first save of the turn loses a race, the retry wins
        calls.append(u["state"])
        if len(calls) == 1:
            raise store.Conflict(P)
        real(u)
    monkeypatch.setattr(bot.db, "save_user", flaky)
    assert "equipo" in say("quiero hablar con una persona")[0]
    assert len(calls) == 2 and bot.db.user(P)["needs_human"] == 1


def test_config_from_s3_is_cached_and_falls_back_to_local(monkeypatch):
    import time
    import boto3
    from moto import mock_aws
    monkeypatch.setattr(bot, "_s3", {"at": 0, "cfg": None})
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("CONFIG_BUCKET", "cfg")
    with mock_aws():
        assert bot.cfg()["business"]["name"]  # bucket missing: local file
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="cfg")
        s3.put_object(Bucket="cfg", Key="lave.yaml", Body=b"business: {name: S3}")
        assert bot.cfg()["business"]["name"] == "S3"
        s3.put_object(Bucket="cfg", Key="lave.yaml", Body=b"business: {name: S3 v2}")
        assert bot.cfg()["business"]["name"] == "S3"  # cached
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() + 61)
        assert bot.cfg()["business"]["name"] == "S3 v2"


def test_lambda_handler_routes_schedule_and_function_url(monkeypatch):
    import time
    import app
    for m in ["pedido", "1", "Calle Falsa 123, Zapopan", "1", "si"]:
        say(m)
    bot.set_status(1, "listo")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 25 * 3600)
    assert app.handler({"source": "aws.events", "detail-type": "Scheduled Event"}, None)["reviews"] == 1
    monkeypatch.setenv("WA_VERIFY_TOKEN", "vt")
    url_event = {"version": "2.0", "rawPath": "/webhook", "rawQueryString": "hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42",
                 "headers": {"host": "x.lambda-url.us-east-1.on.aws"}, "isBase64Encoded": False,
                 "requestContext": {"http": {"method": "GET", "path": "/webhook", "sourceIp": "1.2.3.4", "protocol": "HTTP/1.1"}}}
    r = app.handler(url_event, None)
    assert r["statusCode"] == 200 and r["body"] == "42"


def test_bedrock_backend_keeps_no_se_contract(monkeypatch):
    import boto3
    import llm
    monkeypatch.setenv("LLM_FALLBACK", "1")
    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    reply = {}
    class FakeBedrock:
        def converse(self, **kw):
            assert kw["inferenceConfig"]["maxTokens"] <= 512 and "NO_SE" in kw["system"][0]["text"]
            return {"stopReason": reply["stop"], "output": {"message": {"content": [{"text": reply["text"]}]}}}
    monkeypatch.setattr(boto3, "client", lambda svc, **kw: FakeBedrock())
    c = bot.cfg()
    reply.update(stop="end_turn", text="Abrimos a las 8 😊")
    assert llm.llm_answer("¿a qué hora abren?", c) == "Abrimos a las 8 😊"
    reply.update(text="NO_SE")
    assert llm.llm_answer("¿venden pizza?", c) is None
    reply.update(stop="max_tokens", text="Abrimos a")
    assert llm.llm_answer("¿a qué hora abren?", c) is None
