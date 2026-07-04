"""The curio-cabinet command line interface."""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import click

from . import __version__
from .config import ConfigError
from .db import connect, ensure_engine_tables
from .instance import resolve_instance

INIT_ENV_TEMPLATE = """\
# Curio-Cabinet instance settings. Keep this file out of version control.
SECRET_KEY={secret}
# CABINET_JOURNAL_MODE=WAL          # use TRUNCATE on network filesystems
# CABINET_COOKIE_SECURE=1           # leave on except plain-HTTP local dev
"""

INIT_CONFIG_TEMPLATE = """\
collection:
  title: "My Collection"
  slug: items
  id: {strategy: sequential, width: 4}
  title_field: name
  default_sort: {field: name, order: asc}

fields:
  - key: name
    label: Name
    type: text
    required: true
    searchable: true
    views: {table: true}

  - key: notes
    label: Notes
    type: longtext
    searchable: true

groups:
  - key: core
    label: Overview
    fields: [name, notes]
"""


@click.group()
@click.version_option(__version__, prog_name="curio-cabinet")
@click.option(
    "--instance",
    "instance_root",
    envvar="CABINET_INSTANCE",
    default="instance",
    show_default=True,
    help="Path to the instance directory.",
)
@click.pass_context
def main(ctx: click.Context, instance_root: str) -> None:
    ctx.obj = instance_root


def _instance(ctx: click.Context):
    try:
        return resolve_instance(ctx.obj)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from None


def _open(inst):
    conn = connect(inst.db_path, journal_mode=inst.journal_mode)
    ensure_engine_tables(conn)
    return conn


@main.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Create a new instance directory with a starter config."""
    root = Path(ctx.obj).resolve()
    config_path = root / "collection.yaml"
    if config_path.exists():
        raise click.ClickException(f"{config_path} already exists")
    (root / "data" / "backups").mkdir(parents=True, exist_ok=True)
    (root / "images").mkdir(exist_ok=True)
    config_path.write_text(INIT_CONFIG_TEMPLATE, encoding="utf-8")
    env_path = root / ".env"
    if not env_path.exists():
        env_path.write_text(
            INIT_ENV_TEMPLATE.format(secret=secrets.token_urlsafe(32)),
            encoding="utf-8",
        )
        env_path.chmod(0o600)
    click.echo(f"Instance created at {root}")
    click.echo("Next: edit collection.yaml, then run `curio-cabinet migrate` "
               "and `curio-cabinet create-admin`.")


@main.command()
@click.pass_context
def check(ctx: click.Context) -> None:
    """Validate config, report schema drift and data conformance."""
    from .coerce import coerce_value, CoercionError
    from .schema import detect_drift

    inst = _instance(ctx)
    click.echo(f"config OK: {len(inst.registry.fields)} fields, "
               f"{len(inst.registry.groups)} groups")

    if not inst.db_path.exists():
        click.echo("database: not created yet (run `curio-cabinet migrate`)")
        return

    conn = _open(inst)
    drift = detect_drift(conn, inst.registry)
    click.echo(f"schema: {drift.describe()}")

    # Data conformance: values that would fail today's validation rules.
    bad: dict[str, int] = {}
    required_null: dict[str, int] = {}
    if drift.kind in ("match", "additive"):
        for row in conn.execute(f'SELECT * FROM "{inst.registry.table}"'):
            item = dict(row)
            for f in inst.registry.fields:
                value = item.get(f.key)
                if value is None:
                    if f.required:
                        required_null[f.key] = required_null.get(f.key, 0) + 1
                    continue
                try:
                    coerce_value(f, value)
                except CoercionError:
                    bad[f.key] = bad.get(f.key, 0) + 1
    for key, n in sorted(required_null.items()):
        click.echo(f"data: {key}: {n} rows missing a required value")
    for key, n in sorted(bad.items()):
        click.echo(f"data: {key}: {n} rows fail current validation")
    if not bad and not required_null:
        click.echo("data: OK")
    stale = [
        f.key for f in inst.registry.fields
        if f.rename_from and drift.kind != "destructive"
    ]
    for key in stale:
        click.echo(f"hint: field {key!r} has a stale rename_from — safe to delete")


@main.command()
@click.option("--force", is_flag=True, help="Store NULL for values that fail coercion.")
@click.pass_context
def migrate(ctx: click.Context, force: bool) -> None:
    """Apply the config to the database (rebuild; backs up first)."""
    from .schema import SchemaError, backup_database, detect_drift, rebuild

    inst = _instance(ctx)
    conn = _open(inst)
    drift = detect_drift(conn, inst.registry)
    if drift.kind == "match":
        click.echo("schema already matches config")
        return
    if inst.db_path.exists() and drift.kind != "fresh":
        backup = backup_database(inst.db_path, inst.backups_dir)
        click.echo(f"backup: {backup}")
    try:
        warnings = rebuild(conn, inst.registry, force=force)
    except SchemaError as exc:
        raise click.ClickException(str(exc)) from None
    for w in warnings:
        click.echo(f"warning: {w}")
    click.echo(f"migrated: {drift.describe()}")


@main.command("import-csv")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True)
@click.pass_context
def import_csv_cmd(ctx: click.Context, path: str, dry_run: bool) -> None:
    """Import rows from a CSV file (header = field keys or labels)."""
    from .csvio import import_csv

    inst = _instance(ctx)
    conn = _open(inst)
    report = import_csv(
        conn, inst.registry, Path(path).read_text(encoding="utf-8-sig"),
        dry_run=dry_run,
    )
    for note in report.notes:
        click.echo(f"note: {note}")
    for err in report.errors[:20]:
        click.echo(f"error: {err}")
    if len(report.errors) > 20:
        click.echo(f"... and {len(report.errors) - 20} more errors")
    verb = "would import" if dry_run else "imported"
    click.echo(f"{verb} {report.imported} rows, skipped {report.skipped}")


@main.command("export-csv")
@click.argument("path", type=click.Path(dir_okay=False), required=False)
@click.pass_context
def export_csv_cmd(ctx: click.Context, path: str | None) -> None:
    """Export all rows as CSV (stdout if no path given)."""
    from .csvio import export_csv

    inst = _instance(ctx)
    conn = _open(inst)
    text = export_csv(conn, inst.registry)
    if path:
        Path(path).write_text(text, encoding="utf-8")
        click.echo(f"wrote {path}")
    else:
        sys.stdout.write(text)


@main.command("create-admin")
@click.pass_context
def create_admin(ctx: click.Context) -> None:
    """Create the admin account (refuses if one exists)."""
    from .auth import create_admin_user, UserExistsError

    inst = _instance(ctx)
    conn = _open(inst)
    username = click.prompt("Username")
    password = click.prompt(
        "Password", hide_input=True, confirmation_prompt=True
    )
    try:
        create_admin_user(conn, username, password)
    except UserExistsError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"admin {username!r} created. Enable TOTP from /admin/settings.")


@main.command("reset-password")
@click.pass_context
def reset_password(ctx: click.Context) -> None:
    """Reset the admin password (recovery path; requires shell access)."""
    from .auth import reset_admin_password

    inst = _instance(ctx)
    conn = _open(inst)
    password = click.prompt(
        "New password", hide_input=True, confirmation_prompt=True
    )
    username = reset_admin_password(conn, password)
    click.echo(f"password reset for {username!r}; all sessions logged out")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--debug", is_flag=True)
@click.pass_context
def run(ctx: click.Context, host: str, port: int, debug: bool) -> None:
    """Run the development server (use gunicorn in production)."""
    from .app import create_app

    app = create_app(instance_root=ctx.obj)
    if debug:
        app.config["TEMPLATES_AUTO_RELOAD"] = True
    # threaded so a browser's parallel asset requests don't serialize
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
