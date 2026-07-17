#!/usr/bin/env bash
# Pull-based continuous deployment for the whole stack. Run by the
# finrepin-deploy systemd timer (see the unit files next to this script);
# idempotent, so it is safe to run by hand at any time.
#
#   1. Fast-forward this checkout to origin/main. The server clone is a
#      deploy artifact, not a workspace: local edits to tracked files are
#      discarded. Untracked files (.env, backups/) are never touched.
#   2. Pull newer images — the app image CI publishes on every green main
#      build, plus any db/grafana tag bumps that arrived via git.
#   3. Re-run `compose up`, which recreates only containers whose image or
#      configuration changed. Grafana dashboard/provisioning changes arrive
#      through the bind mounts without any restart.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

compose() {
    docker compose -f compose.yaml -f compose.prod.yaml "$@"
}

git fetch --quiet origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
    echo "Updating checkout $(git rev-parse --short HEAD) -> $(git rev-parse --short origin/main)"
    git reset --quiet --hard origin/main
fi

compose pull --quiet
compose up -d --remove-orphans
