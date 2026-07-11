# Mazag API

FastAPI backend: chatbot (Gemini), order creation + customer upsert, order tracking. SQLite storage.

## Deploy (droplet)

```bash
sudo mkdir -p /opt/mazag && cd /opt/mazag
git clone git@github.com:<you>/mazag.git .   # or git pull if it exists
cd api
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env             # real key + origins. NEVER commit .env
```

systemd — `/etc/systemd/system/mazag-api.service`:

```ini
[Unit]
Description=Mazag API
After=network.target

[Service]
WorkingDirectory=/opt/mazag/api
ExecStart=/opt/mazag/api/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8010
Restart=always
EnvironmentFile=/opt/mazag/api/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now mazag-api
curl localhost:8010/api/health
```

Caddy — serves the site + API on one domain (no CORS needed):

```
mazag.yourdomain.com {
    handle /api/* {
        reverse_proxy 127.0.0.1:8010
    }
    handle {
        root * /opt/mazag
        file_server
    }
}
```

Then in `index.html` and `track.html` set `API_BASE = ""` (same origin) — or the full
API URL if the frontend stays on GitHub Pages (keep ALLOWED_ORIGINS strict in that case).

## Endpoints
- `POST /api/chat` — `{message, session_id}` → `{reply, session_id}`
- `POST /api/orders` — order payload → `{order_code, total, status}`
- `GET /api/orders/track?phone=&code=` → `{status, eta}`
- `GET /api/health`