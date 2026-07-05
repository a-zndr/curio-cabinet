# Deploying

## NearlyFreeSpeech.net (subdomain)

This is the setup the project was built for: a Python daemon behind NFS's
proxy, TLS handled automatically. All member-UI steps are one-time.

### 1. Create the site

- Add a new site in the member UI. Add your subdomain (e.g.
  `toys.example.com`) as an **alias**; since NFS hosts your DNS, this creates
  the CNAME automatically, and **TLS is provisioned automatically** — no
  `tls-setup.sh`.

### 2. Lay out the files

```
/home/protected/app/       <- git checkout of this repo (code only)
/home/protected/venv/      <- python -m venv; pip install -e app[serve,heic]
/home/protected/data/      <- instance: collection.yaml, catalog.db, images/, backups/
/home/protected/env        <- chmod 600; SECRET_KEY=..., CABINET_JOURNAL_MODE=WAL
/home/public/              <- leave empty (nothing sensitive is web-reachable)
```

Generate a secret: `python -c "import secrets;print(secrets.token_urlsafe(32))"`.

### 3. Add the daemon and proxy

- **Daemons → Add a Daemon**: command
  `/home/protected/app/deploy/nfs/run.sh`, user `web`. NFS supervises it and
  restarts on crash/reboot. The script runs gunicorn in the foreground with a
  single worker on `127.0.0.1:8099`.
- **Add a Proxy**: protocol HTTP, target port `8099`, path `/`.

### 4. First-run

```bash
ssh you_site@ssh.<region>.nearlyfreespeech.net
cd /home/protected
python3 -m venv venv
venv/bin/pip install -e "app[serve]"        # add ,heic if libheif is available
CABINET_INSTANCE=/home/protected/data venv/bin/curio-cabinet migrate
CABINET_INSTANCE=/home/protected/data venv/bin/curio-cabinet create-admin
```

Start the daemon from the UI.

### 5. Backups

**Scheduled Tasks → add** `/home/protected/app/deploy/nfs/backup.sh` daily. It
writes verified, gzipped `VACUUM INTO` snapshots and prunes to 14 daily + 8
weekly. Pull them to your Mac periodically (the server is now the source of
truth):

```bash
rsync -az you_site@ssh.<region>.nearlyfreespeech.net:/home/protected/data/ ~/Backups/curio/
```

### 6. Deploys

From your Mac, after committing code changes:

```bash
NFS_SSH=you_site@ssh.<region>.nearlyfreespeech.net ./deploy/deploy.sh
```

It rsyncs **code only** (never your data), reinstalls, runs `check`, and
reminds you to restart the daemon. Additive schema changes apply themselves at
the next boot; destructive ones need an explicit `curio-cabinet migrate` over
SSH.

### Notes for NFS

- C-extension wheels aren't published for FreeBSD, so `pip` builds
  `argon2-cffi` and `Pillow` from source. If a build fails, NFS will
  centrally install stubborn packages on request (free).
- HEIC needs `libheif`; if it isn't available, leave the `heic` extra off —
  the app rejects HEIC uploads with a clear message and iOS Safari transcodes
  to JPEG on upload anyway.
- If WAL misbehaves on their storage, set `CABINET_JOURNAL_MODE=TRUNCATE` in
  `/home/protected/env`.

## Docker (anywhere else)

```bash
mkdir -p instance && cp examples/impact-toys/collection.yaml instance/collection.yaml
echo "SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" > instance/.env

docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml run --rm web curio-cabinet migrate
docker compose -f deploy/docker-compose.yml run --rm web curio-cabinet create-admin
docker compose -f deploy/docker-compose.yml up -d
```

The image installs `libheif`, so HEIC uploads work. Put it behind a reverse
proxy (Caddy, nginx, Traefik) that terminates TLS and forwards to port 8099.
