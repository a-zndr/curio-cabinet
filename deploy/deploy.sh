#!/bin/sh
# Deploy the engine code to NearlyFreeSpeech from your Mac.
#
# Ships CODE ONLY — never the instance (config/db/images live on the server
# and are the source of truth). Configure the SSH host in ~/.ssh/config, then:
#
#   ./deploy/deploy.sh
#
# Set NFS_SSH to your "user_site@ssh.phx.nearlyfreespeech.net" target.

set -e

NFS_SSH="${NFS_SSH:?set NFS_SSH to your NFS ssh target}"
REMOTE_APP="/home/protected/app"
REMOTE_VENV="/home/protected/venv"
HERE="$(cd "$(dirname "$0")/.." && pwd)"

echo "→ syncing code to $NFS_SSH:$REMOTE_APP"
rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    --exclude 'instance' --exclude 'tests' --exclude '*.pyc' \
    "$HERE/" "$NFS_SSH:$REMOTE_APP/"

echo "→ installing deps and applying additive schema drift"
ssh "$NFS_SSH" "
  set -e
  '$REMOTE_VENV/bin/pip' install -q -e '$REMOTE_APP'
  export CABINET_INSTANCE=/home/protected/data
  '$REMOTE_VENV/bin/curio-cabinet' check
"

echo "→ restart the 'curio' daemon from the NFS member UI (Daemons box)"
echo "  (or: ssh in and the supervisor picks up a SIGHUP if you send one)"
echo "done."
