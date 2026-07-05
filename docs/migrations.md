# Migrations

The config is the source of truth for the schema. There is exactly one
migration mechanism — **rebuild** — and one command:

```bash
curio-cabinet migrate
```

## How it works

On `migrate` (and at every boot), Curio-Cabinet compares your `collection.yaml`
against the live database and against a logical snapshot it recorded the last
time the schema was applied (stored in the `_meta` table). This catches changes
that SQLite's column types alone cannot see — e.g. `longtext` → `tags` (both
are TEXT) or changing a field's stored unit.

Drift is classified as:

- **fresh** — no table yet; it's created.
- **match** — nothing to do.
- **additive** — only new fields added. Applied automatically, including at
  boot, so a config push never takes the site down.
- **destructive** — a field was removed, retyped, renamed, or its unit changed.
  The app refuses to start on this; you run `curio-cabinet migrate` explicitly.

Every non-trivial migration:

1. Takes a **verified backup first** (`VACUUM INTO` + `PRAGMA integrity_check`)
   into `instance/data/backups/` — never a raw file copy, which can be torn
   while the database is in use.
2. Rebuilds the table in a single transaction, copying rows across. Only
   columns that actually changed are re-coerced; untouched columns copy
   verbatim, so a migration can't choke on old data in unrelated fields.
3. Aborts if any changed value fails coercion — unless you pass `--force`,
   which stores `NULL` for those values and reports each one.

## Renaming a field

Add a `rename_from` hint so data carries across instead of being dropped and
re-added:

```yaml
- key: heel_knot_diameter
  label: Heel Knot Diameter
  type: number
  rename_from: heal_knot_d      # the old key
```

The hint is consumed only when the old column exists and the new one doesn't;
once migrated you can delete it. `curio-cabinet check` warns about stale hints.

## Checking data health

```bash
curio-cabinet check
```

reports config validity, schema drift, and any existing rows that would fail
current validation (e.g. after you tighten an enum). Fixing those is a data
edit, not a schema migration.

## Restoring a backup

Stop the app, replace `instance/data/catalog.db` with the (gunzipped) backup,
delete any stray `catalog.db-wal` / `catalog.db-shm`, and start again.
