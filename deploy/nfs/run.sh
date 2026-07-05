#!/bin/sh
# NearlyFreeSpeech daemon run script.
#
# Register in the member UI: Site Information -> Daemons -> Add a Daemon
#   Tag:              curio
#   Command:          /home/protected/app/deploy/nfs/run.sh
#   User:             your member user (NOT web) — it must write the SQLite DB
#                     and uploaded images under /home/protected/data, which are
#                     owned by you. A web-user daemon would be read-only there.
# Then add a Proxy: Site Information -> Add a Proxy
#   Protocol HTTP, port 8099, path /
#
# The daemon MUST run in the foreground; NFS supervises and restarts it.

set -e

INSTANCE="/home/protected/data"
VENV="/home/protected/venv"

# secrets & settings (SECRET_KEY, CABINET_JOURNAL_MODE, ...)
. /home/protected/env

export CABINET_INSTANCE="$INSTANCE"

# Single writer process. NFS storage is network-attached, so WAL's shared
# memory is unreliable across processes — one worker with threads keeps a
# single writer and sidesteps it. Bump threads, not workers.
exec "$VENV/bin/gunicorn" \
    --workers 1 \
    --threads 8 \
    --worker-class gthread \
    --bind 127.0.0.1:8099 \
    --timeout 60 \
    --access-logfile /home/logs/curio-access.log \
    --error-logfile /home/logs/curio-error.log \
    "curio_cabinet.app:create_app()"
