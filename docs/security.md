# Security model

Curio-Cabinet is built for a single trusted admin and a public, read-only
audience. The design choices below reflect that threat model.

## Authentication

- **One admin, no signup.** The account is created only via
  `curio-cabinet create-admin` (refuses if one exists). There is no
  registration route and no email/password reset flow.
- **Passwords** are hashed with **argon2id** (RFC 9106 low-memory profile) and
  transparently re-hashed on login if parameters change. Minimum length 12.
- **TOTP** two-factor is optional, enrolled from Settings with a QR code and
  confirmed with a live code before it's enabled. Used codes can't be replayed
  within their time window.
- **Sessions** are server-side. The cookie holds a random token; the database
  stores only its SHA-256, so a leaked database or backup cannot hijack a live
  session. Cookies are `__Host-` prefixed, `Secure`, `HttpOnly`, `SameSite=Lax`.
  Sessions expire (7-day absolute, 24-hour idle); changing your password logs
  out every device.
- **Login throttling** is a progressive delay per username, checked before any
  password hashing. Because the admin username is fixed and guessable, a device
  that has logged in before carries a signed cookie that bypasses the throttle —
  so an attacker hammering the username can slow only themselves, never lock
  the real admin out. Unknown usernames still run a dummy hash so timing doesn't
  reveal whether an account exists.

## Recovery

If you lose your password (and TOTP), reset it from a shell on the server:

```bash
curio-cabinet reset-password
```

This is the **only** recovery path. Losing both your credentials and server
access is unrecoverable by design — there is no backdoor.

## Uploads

Uploaded images are never trusted or stored as-is. Each is sniffed by magic
bytes, verified and re-decoded by Pillow, EXIF/GPS stripped, converted to RGB,
and **re-encoded** — the stored bytes are always our own output, which defeats
polyglot files and metadata leaks. A pixel cap rejects decompression bombs.
Files are named by content hash (never by user input), and the public image
route validates the hash and variant against strict patterns before touching
the filesystem.

## Web hardening

- No request string ever becomes a SQL identifier: sort/filter/pivot fields are
  matched against the config registry, and all values are bind parameters.
- Strict `Content-Security-Policy` (`script-src 'self'`, no `unsafe-eval`),
  plus `nosniff`, `Referrer-Policy`, `frame-ancestors 'none'`, and HSTS.
- All mutations are POST with a per-session CSRF token (synchronizer pattern),
  verified in constant time.
- Jinja autoescaping is on everywhere; user-supplied text is never marked safe.
  The share-link title is shown in the page body but never placed in OpenGraph
  tags, so it can't be used to spoof link previews under your domain.

## SQLite on networked storage

On hosts where the database lives on network-attached storage (e.g.
NearlyFreeSpeech), WAL's shared-memory index is unreliable across processes.
Run a **single writer process** (one gunicorn worker, scale with threads) and,
if WAL misbehaves, set `CABINET_JOURNAL_MODE=TRUNCATE` in the instance `.env`.

## Reporting

Found something? Open a private security advisory on the GitHub repo rather
than a public issue.
