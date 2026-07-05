# Deploying

## NearlyFreeSpeech.net (subdomain)

This is the setup the project was built for: a Python daemon behind NFS's
proxy, TLS handled automatically. All member-UI steps are one-time.

### 1. Create the site + point DNS at it

- Add a new site in the member UI and add your subdomain (e.g.
  `toys.example.com`) as an **alias** on it. This tells NFS to serve that
  hostname and to request a TLS cert for it.
- **DNS:** if NFS hosts your domain's DNS, adding the alias creates the record
  automatically. **If DNS is managed elsewhere** (e.g. Hover, Cloudflare, your
  registrar), you must add the record there yourself: create a **CNAME** for
  the subdomain pointing at the target NFS shows in the site's Information
  panel (typically `yourshortname.nfshost.com`). Use a CNAME for a subdomain;
  only the apex needs A/AAAA records.
- **TLS is automatic but only after the record resolves to NFS.** Let's Encrypt
  validates over HTTP, so the cert is issued once your CNAME propagates and
  traffic reaches the site (usually minutes to an hour). No `tls-setup.sh`.
  Leave `CABINET_COOKIE_SECURE` at its secure default and just wait for HTTPS
  to come up before logging into `/admin` — the public pages work over HTTP in
  the meantime, but the admin session cookie is (correctly) HTTPS-only.

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
  `/home/protected/app/deploy/nfs/run.sh`, run as **your member user, not
  `web`** — the daemon writes the SQLite DB and uploaded images under
  `/home/protected/data` (owned by you), so a `web` daemon would be read-only
  and logins/edits would fail. NFS supervises it and restarts on crash/reboot.
  The script runs gunicorn in the foreground with a single worker on
  `127.0.0.1:8099`.
- **Add a Proxy**: protocol HTTP, target port `8099`, path `/`.

### 4. Seed your data (one-time)

The deploy script ships **code only**, so your existing collection (config +
database + images) has to go up once. From your Mac, push your local instance
into `/home/protected/data`, excluding the dev `.env` (the server gets its own):

```bash
rsync -az --exclude .env \
    ~/.curio-cabinet/toys/ \
    you_site@ssh.<region>.nearlyfreespeech.net:/home/protected/data/
```

Starting fresh instead of migrating an existing collection? Skip this and run
`curio-cabinet migrate` on the server in the next step to create an empty DB.

After this one-time seed the **server is the source of truth**: deploys carry
code only, and backups flow back down to your Mac (step 6).

### 5. First-run

```bash
ssh you_site@ssh.<region>.nearlyfreespeech.net
cd /home/protected
python3 -m venv venv
venv/bin/pip install -e "app[serve]"        # add ,heic if libheif is available
export CABINET_INSTANCE=/home/protected/data
venv/bin/curio-cabinet migrate               # applies any schema drift (no-op on a seeded DB)
venv/bin/curio-cabinet reset-password        # set a real admin password (the seeded DB carried your local temp one)
```

If you started fresh in step 4 instead of seeding, run `create-admin` here
rather than `reset-password`. Start the daemon from the UI, then enable TOTP
from **Settings** once the site is reachable.

### 6. Backups

**Scheduled Tasks → add** `/home/protected/app/deploy/nfs/backup.sh` daily. It
writes verified, gzipped `VACUUM INTO` snapshots and prunes to 14 daily + 8
weekly. Pull them to your Mac periodically (the server is now the source of
truth):

```bash
rsync -az you_site@ssh.<region>.nearlyfreespeech.net:/home/protected/data/ ~/Backups/curio/
```

### 7. Deploys

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
mkdir -p instance && cp examples/camera-lenses/collection.yaml instance/collection.yaml
echo "SECRET_KEY=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" > instance/.env

docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml run --rm web curio-cabinet migrate
docker compose -f deploy/docker-compose.yml run --rm web curio-cabinet create-admin
docker compose -f deploy/docker-compose.yml up -d
```

The image installs `libheif`, so HEIC uploads work. Put it behind a reverse
proxy (Caddy, nginx, Traefik) that terminates TLS and forwards to port 8099.
