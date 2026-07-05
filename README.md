# Curio-Cabinet

A self-hosted, config-driven catalog for the things you collect.

Describe your collection's fields once in a `collection.yaml`, and
Curio-Cabinet builds everything from it: the database schema, the browse /
table / pivot views, filters, the admin editing forms, image handling, and
a share-by-link feature. Single-admin authentication, SQLite storage, and
no Node toolchain — `pip install` and go.

It started as a database of impact toys; the engine knows nothing about
toys. Point it at a different config and it's a catalog of camera lenses,
houseplants, vinyl records, or whatever you collect.

## Features

- **One config drives everything.** Add a field in `collection.yaml`, and it
  flows to the table, cards, filters, detail page, pivot, and admin form with
  no template or code changes. Fields have types (text, number with units,
  enum, tags, boolean, url, date…), units with automatic conversion (store cm,
  display inches), and per-field view rules.
- **Edit from the browser.** Secure single-admin auth (argon2id password +
  optional TOTP), add/edit/delete items, and upload photos — no more editing
  files and committing to publish.
- **Share a selection by link.** Anyone (no account) can select N items and
  send a URL; the link encodes the selection itself, so there's no server
  state and it works forever.
- **Mobile-first, light + dark**, professional design out of the box, fully
  re-themeable from the config.
- **Safe by construction.** No request string ever reaches SQL as an
  identifier; uploads are re-encoded through Pillow (EXIF/GPS stripped,
  polyglots defeated); strict Content-Security-Policy; server-side sessions
  storing only token hashes.

## Quick start

```bash
pip install -e ".[serve]"      # add ,heic for HEIC uploads
curio-cabinet init             # scaffolds ./instance
# edit instance/collection.yaml to describe your collection
curio-cabinet migrate          # build the database from the config
curio-cabinet create-admin     # create your login
curio-cabinet run              # dev server at http://127.0.0.1:8080
```

Two example configs live in [`examples/`](examples/): the original impact-toys
collection and a contrasting camera-lens one.

## Documentation

- [Quick start](docs/quickstart.md)
- [Config reference](docs/config-reference.md) — every field option
- [Migrations](docs/migrations.md) — how schema changes are applied
- [Deploying](docs/deploy-nfs.md) — NearlyFreeSpeech and Docker
- [Security model](docs/security.md)

## License

MIT — see [LICENSE](LICENSE).
