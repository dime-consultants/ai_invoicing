# Docker Deployment — invoicing.dimeconsultants.africa

Django + Daphne + PostgreSQL + Redis + Nginx, all in Docker.  
SSL terminated by host Nginx (same pattern as the frontend).

---

## Project Structure

```
ai_invoicing/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt          ← cleaned (no CUDA/spaCy)
├── .env.example              ← commit this
├── .env                      ← DO NOT commit — create on server
├── nginx/
│   └── invoicing.conf        ← Docker Nginx config (HTTP only)
└── deploy/
    ├── host-nginx.conf       ← paste onto VPS host Nginx
    └── README.md             ← this file
```

---

## Architecture

```
Browser (HTTPS 443)
        ↓
Host Nginx — terminates SSL, proxies to port 6081
        ↓
Docker Nginx :6081 — serves /static/ and /media/ directly
        ↓
Daphne :8000 — Django ASGI (HTTP + WebSocket)
        ↓
PostgreSQL :5432      Redis :6379
```

---

## First-Time Deployment

### 1. DNS — do this first

Add an A record at your registrar:
- **Name**: `invoicing`
- **Type**: `A`
- **Value**: your VPS IP
- **TTL**: 3600

Check it resolves before running certbot:
```bash
nslookup invoicing.dimeconsultants.africa
```

### 2. Clone repo on VPS

```bash
git clone <your-repo-url> /root/ai_invoicing
cd /root/ai_invoicing
```

### 3. Create .env

```bash
cp .env.example .env
nano .env
```

Fill in every `CHANGE_ME` value. Generate a secret key:
```bash
python3 -c "import secrets; print(secrets.token_hex(50))"
```

### 4. Run migrations + start containers

```bash
# Run migrations first (one-shot container, exits when done)
docker-compose run --rm migrate

# Start everything
docker-compose up -d

# Check all containers are healthy
docker-compose ps
```

All four containers should show `Up (healthy)` within ~60 seconds.

### 5. Setup host Nginx + SSL

```bash
# Install certbot if not already present
apt install certbot python3-certbot-nginx -y

# Copy host Nginx config
cp deploy/host-nginx.conf /etc/nginx/sites-available/invoicing
ln -s /etc/nginx/sites-available/invoicing /etc/nginx/sites-enabled/invoicing
nginx -t && systemctl reload nginx

# Issue SSL certificate
certbot --nginx -d invoicing.dimeconsultants.africa \
  --non-interactive --agree-tos -m your@email.com

systemctl reload nginx
```

### 6. Verify

```bash
curl -I https://invoicing.dimeconsultants.africa/health/
# → HTTP/2 200

docker-compose ps
# → all containers: Up (healthy)
```

---

## Updating the App

```bash
cd /root/ai_invoicing
git pull

# Rebuild app image, run migrations, restart
docker-compose build app
docker-compose run --rm migrate
docker-compose up -d app

# Or full restart if you changed compose/nginx:
docker-compose down && docker-compose up -d
```

---

## Daily Commands

```bash
# Live logs
docker-compose logs -f app
docker-compose logs -f nginx

# Container status
docker-compose ps

# Restart one service
docker-compose restart app

# Open Django shell
docker-compose exec app python manage.py shell

# Run a management command
docker-compose exec app python manage.py createsuperuser

# Full restart
docker-compose down && docker-compose up -d
```

---

## Troubleshooting

### 502 Bad Gateway
App container is unhealthy or not running.
```bash
docker-compose ps
docker-compose logs app --tail 50
docker-compose restart app
```

### Migrations failed on startup
```bash
docker-compose logs migrate
# Fix the issue, then re-run:
docker-compose run --rm migrate
```

### Static files 404
Static files are served from a Docker volume. Make sure collectstatic ran:
```bash
docker-compose run --rm migrate   # collectstatic runs here too
```

### WebSockets not connecting
Check the `/ws/` location block is in both nginx configs and that Nginx is reloaded:
```bash
docker-compose logs nginx
systemctl reload nginx
```

### Database connection refused
```bash
docker-compose logs db
docker-compose ps db
# DB must be healthy before app starts
docker-compose restart db
sleep 10
docker-compose restart app
```

### Container stuck in "health: starting"
```bash
# Check what the healthcheck is actually hitting
docker inspect invoicing-app | grep -A 10 Health
docker-compose logs app --tail 30
```

### SSL certificate issues
```bash
certbot certificates                          # check expiry
certbot renew --dry-run                       # test auto-renewal
certbot --nginx -d invoicing.dimeconsultants.africa  # re-issue
```

---

## Environment Variables

| Variable | Required | Notes |
|----------|----------|-------|
| `SECRET_KEY` | ✅ | Long random string, keep secret |
| `DEBUG` | ✅ | Must be `False` in production |
| `DB_NAME` | ✅ | |
| `DB_USER` | ✅ | |
| `DB_PASSWORD` | ✅ | |
| `DB_HOST` | ✅ | Always `db` inside Docker |
| `DB_PORT` | ✅ | Always `5432` |
| `REDIS_URL` | ✅ | Always `redis://redis:6379/0` inside Docker |
| `XAI_API_KEY` | ⚠️ | Required for AI features |
| `UNSTRUCTURED_API_KEY` | ⚠️ | Leave blank to use local package |

---

## Volumes (persistent data)

| Volume | What's in it |
|--------|-------------|
| `postgres_data` | All database data |
| `redis_data` | Redis persistence |
| `static_files` | Django collectstatic output |
| `media_files` | User-uploaded files |

To back up the database:
```bash
docker-compose exec db pg_dump -U $DB_USER $DB_NAME > backup.sql
```

To restore:
```bash
docker-compose exec -T db psql -U $DB_USER $DB_NAME < backup.sql
```

---

*Domain: invoicing.dimeconsultants.africa — Updated 2026*