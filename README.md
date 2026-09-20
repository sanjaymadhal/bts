# Trackr Backend

FastAPI app + driver simulator, deployed as a single Render Web Service.

See:
- `docs/superpowers/specs/2026-08-09-trackr-backend-design.md` — backend spec.
- `docs/superpowers/specs/2026-08-09-trackr-frontend-integration-design.md` — frontend integration spec.
- `migrations/001_init.sql` — Postgres schema for Supabase.

## Local dev

```bash
python -m venv .venv
source .venv/bin/activate          # (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt
cp .env.example .env               # then edit values
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The driver simulator starts automatically when `SIMULATE_DRIVERS=true`.

**Why `--host 0.0.0.0`:** uvicorn's default is `127.0.0.1`, which only
accepts loopback connections. The Expo dev client on a physical phone
can't reach `localhost` on your laptop — bind to all interfaces and
point the app at your LAN IP (`http://192.168.x.x:8000`) instead.

## Tests

```bash
pytest -q
```

Tests use `FakeSupabase` (in `tests/conftest.py`) — no real Supabase project needed.

## Deploy

Push to a git remote, then on Render:

- **Service type:** Web Service (free tier).
- **Build:** `pip install -r requirements.txt`.
- **Start:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.
- **Env vars:** every key in `.env.example`.

Single worker is required — the driver simulator is in-process and would multiply if scaled out.
