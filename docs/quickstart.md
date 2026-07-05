# Quick start

## 1. Install

```bash
git clone https://github.com/zndr/curio-cabinet
cd curio-cabinet
python -m venv .venv && . .venv/bin/activate
pip install -e ".[serve]"     # add ,heic to accept HEIC uploads (needs libheif)
```

## 2. Create an instance

An *instance* is a directory holding your config, database, images, and
secrets — kept separate from the engine code so your data never mixes with the
source.

```bash
curio-cabinet init            # creates ./instance with a starter config + .env
```

Point at a different directory any time with `--instance PATH` or the
`CABINET_INSTANCE` environment variable.

## 3. Describe your collection

Edit `instance/collection.yaml`. Start from an example:

```bash
cp examples/camera-lenses/collection.yaml instance/collection.yaml
```

See the [config reference](config-reference.md) for every option.

## 4. Build the database

```bash
curio-cabinet migrate         # creates/updates tables to match the config
curio-cabinet check           # validates config + reports schema/data issues
```

## 5. Create your admin login

```bash
curio-cabinet create-admin    # prompts for username + password
```

There is no signup page — this CLI is the only way to create the account.
Enable two-factor auth later from **Settings** in the web UI.

## 6. Import existing data (optional)

```bash
curio-cabinet import-csv mydata.csv --dry-run   # preview
curio-cabinet import-csv mydata.csv             # commit
```

The CSV header may use field keys or labels. Values are validated exactly as
admin edits are (units parsed, enums checked, etc.).

## 7. Run

```bash
curio-cabinet run                    # dev server on :8080
curio-cabinet run --debug            # + template auto-reload
```

For production, use gunicorn (a single worker — SQLite is single-writer):

```bash
gunicorn --workers 1 --threads 8 --worker-class gthread \
    --bind 127.0.0.1:8099 "curio_cabinet.app:create_app()"
```

See [deploying](deploy-nfs.md) for NearlyFreeSpeech and Docker.
