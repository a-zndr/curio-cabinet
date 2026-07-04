# Curio-Cabinet

A self-hosted, config-driven catalog for the things you collect.

Define your collection's fields in one `collection.yaml`; Curio-Cabinet
generates the database schema, browse/table/pivot views, filters, admin
editing forms, and image handling from it. Single-admin auth, SQLite
storage, no Node toolchain — `pip install` and go.

**Status: pre-release, under active development.**

```bash
pip install -e .
curio-cabinet init
curio-cabinet migrate
curio-cabinet create-admin
curio-cabinet run
```

Full documentation lives in `docs/` (in progress). Example collection
configs are in `examples/`.

## License

MIT
