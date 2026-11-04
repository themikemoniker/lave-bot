# Lavê WhatsApp bot

Open-source, near-zero-cost WhatsApp bot for Lavê (Zapopan). Rules + YAML config; no RAG, no vector DB. An LLM fallback is optional.

## Roadmap

**Built**
- [x] Welcome menu + keyword FAQ (accents, case and typos tolerated); menus go out as WhatsApp tap-to-choose lists
- [x] Pickup request in Lavê's official steps: service → location (WhatsApp pin or typed address) → time slot → confirm
- [x] Handoff to a person (on request or after 2 unknown messages); bot stays silent until staff reactivate it
- [x] Unanswered questions logged so they can be added to the YAML
- [x] `/admin` page: orders, confirm exact pickup slot (**programado**), mark **listo** and **entregado**, reactivate bot
- [x] Status hook for the ERP (`nuevo programado listo entregado`)
- [x] Customer messages on programado, listo and entregado, review request 24h after listo (or entregado), sent as Meta templates (24h rule)
- [x] Webhook signature check, Meta retry dedupe, `DRY_RUN`, terminal simulator
- [x] Optional LLM fallback that answers only from the YAML
- [x] Docker + cloudflared deploy; showcase PDF generated from real bot output

**To go live**
- [ ] Owner confirms every `TODO_CONFIRM` (prices, slots, payment methods, promos and the review link are placeholders)
- [ ] Meta app, WhatsApp number and a permanent System User token
- [ ] Create the 4 templates in WhatsApp Manager and get them approved
- [ ] Deploy on HTTPS with `ADMIN_TOKEN` and `WA_APP_SECRET` set
- [ ] Decide how staff reply to handed-off chats (WhatsApp Business app coexistence, or a reply box in `/admin`)
- [ ] Connect the ERP to the status hook, or staff use `/admin` only

**Deliveries (return trip)**: today the listo message says staff will write to coordinate the delivery; the bot books only the pickup slot
- [ ] On listo, ask "¿te la llevamos o pasas por ella?" and book a delivery slot
- [ ] Order total and payment due on delivery in the listo message
- [ ] "En camino" message when the driver leaves
- [ ] Coverage-zone check and delivery fee from the shared location
- [ ] Daily route list for the driver: today's pickups and deliveries by slot

**Later**
- [ ] "¿Ya está mi ropa?" answers the order status from the DB
- [ ] Repeat-order shortcut for returning customers (same address and service)

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 16 tests
```
All business text is in `config/lave.yaml` and is re-read on every message, so edits apply without a restart.
**Before going live:** search for `TODO_CONFIRM` (prices, payment methods, promos, review link, address, hours) and fill it in with the owner.

## Try it with no Meta account
```bash
.venv/bin/python sim.py                # chat in the terminal. /loc = share location, /reset = staff reactivates bot
DRY_RUN=1 .venv/bin/uvicorn app:app --reload   # outgoing messages are printed, not sent
open http://localhost:8000/admin       # orders, buttons: programado / listo / entregado, chats waiting for a human, unanswered questions
```
Status hook for the ERP: `curl -XPOST localhost:8000/orders/1/status -H 'content-type: application/json' -d '{"status":"listo"}'`
(statuses: `nuevo programado listo entregado`). On `listo` the customer gets the "ready" message, on `entregado` a thank-you, and the review request goes out `review_delay_hours` later (default 24).

## Env vars
| var | purpose |
|---|---|
| `DRY_RUN=1` | print outgoing messages instead of sending; also skips the webhook signature check |
| `WA_TOKEN`, `WA_PHONE_NUMBER_ID` | WhatsApp Cloud API sending |
| `WA_APP_SECRET` | verifies `X-Hub-Signature-256` on the webhook (required when not in DRY_RUN) |
| `WA_VERIFY_TOKEN` | any string; must match what you enter in the Meta webhook setup |
| `ADMIN_TOKEN` | protects `/admin`, `/orders/*/status`, `/users/*/reset` (open `/admin?token=...`) |
| `DB_PATH` | SQLite file (default `lave.db`) |
| `LLM_FALLBACK=1`, `ANTHROPIC_API_KEY`, `LLM_MODEL` | optional: unknown messages get an answer drawn only from the YAML (default model `claude-opus-5-5`; `claude-haiku-5-5` is ~40x cheaper) |

## Run with Docker (VPS or laptop)
```bash
cp .env.example .env                  # fill it in; keep DRY_RUN=1 to try it first
docker build -t lave-bot .
docker run -d --name lave --restart unless-stopped -p 8000:8000 --env-file .env \
  -v lave-data:/data -v "$PWD/config:/app/config" lave-bot
open http://localhost:8000/admin       # add ?token=... if ADMIN_TOKEN is set
```
The SQLite DB lives in the `lave-data` volume, so it survives rebuilds. Mounting `config/` lets the owner edit `lave.yaml` without rebuilding. Logs (including DRY_RUN messages): `docker logs -f lave`.

**HTTPS for the Meta webhook (free, no open ports):** install [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (`brew install cloudflared` or the `.deb`), then
```bash
cloudflared tunnel --url http://localhost:8000   # prints https://<random>.trycloudflare.com
```
Use `https://<random>.trycloudflare.com/webhook` as the webhook URL (step 3 below). The quick-tunnel URL changes every time cloudflared restarts. For a URL that stays the same, use a free named tunnel on a domain you own in Cloudflare: `cloudflared tunnel login && cloudflared tunnel create lave && cloudflared tunnel route dns lave bot.example.com && cloudflared tunnel run --url http://localhost:8000 lave`.

## Connect real WhatsApp
1. developers.facebook.com → create app → add **WhatsApp** → note the test number's *Phone number ID* and a token (use a System User permanent token for production).
2. Deploy somewhere with HTTPS (any small VPS, or `cloudflared tunnel --url localhost:8000` for testing).
3. WhatsApp → Configuration → Webhook: URL `https://<host>/webhook`, verify token = `WA_VERIFY_TOKEN`; subscribe to `messages`.
4. Run `uvicorn app:app --host 0.0.0.0 --port 8000` with the env vars above (no `DRY_RUN`).
5. **24h rule / templates:** WhatsApp only allows free-form messages within 24h of the customer's last message, and "programado", "listo", "entregado" and the review request often land later. They are sent as Meta-approved templates listed under `wa_templates` in `config/lave.yaml`. In WhatsApp Manager > Message templates, create each one (category *Utility*, Spanish (MEX)) with the wording from `templates`, using `{{1}}`, `{{2}}`… in `params` order. Until they're approved, those sends fail and are logged as `[send error]`; customer replies inside the 24h window still work. In `DRY_RUN` they print as plain text.

## Privacy
We store only phone, WhatsApp profile name, the location/address, and order details. The welcome message says so.
