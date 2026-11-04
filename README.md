# Lavê WhatsApp bot

Open-source, near-zero-cost WhatsApp bot for Lavê (Zapopan). Rules + YAML config; no RAG, no vector DB. An LLM fallback is optional.

## Setup
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 10 tests
```
All business text is in `config/lave.yaml` and is re-read on every message, so edits apply without a restart.
**Before going live:** search for `TODO_CONFIRM` (prices, payment methods, promos, review link, address, hours) and fill it in with the owner.

## Try it with no Meta account
```bash
.venv/bin/python sim.py                # chat in the terminal. /loc = share location, /reset = staff reactivates bot
DRY_RUN=1 .venv/bin/uvicorn app:app --reload   # outgoing messages are printed, not sent
open http://localhost:8000/admin       # orders, buttons: programado / listo, chats waiting for a human, unanswered questions
```
Status hook for the ERP: `curl -XPOST localhost:8000/orders/1/status -H 'content-type: application/json' -d '{"status":"listo"}'`
(statuses: `nuevo programado listo entregado`). On `listo` the customer gets the "ready" message, and the review request goes out `review_delay_hours` later (default 24).

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
5. **24h rule:** WhatsApp only allows free-form messages within 24h of the customer's last message. The "listo" and review messages may land outside that window, so register them as approved *templates* in Meta and switch `bot.send` to template sends for those two.

## Privacy
We store only phone, WhatsApp profile name, the location/address, and order details. The welcome message says so.
