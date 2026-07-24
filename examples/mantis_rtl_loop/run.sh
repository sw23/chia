#!/usr/bin/env bash
# Submit the Mantis loop as a chia job so its driver logs show in the dashboard
# (http://localhost:8265). Running `python mantis_loop.py` directly also works for
# debugging but registers a DRIVER job the dashboard doesn't capture.
#
#   ./run.sh                       # full loop with config.py defaults
#   ./run.sh --max-iters 1         # one pass
#   ./run.sh --stages architecture,threat_model,plan,researcher,dedupe
#   ./run.sh --dry-run             # print the plan and exit (no cluster work)
set -euo pipefail
cd "$(dirname "$0")"

PY="${MANTIS_LOOP_PY:-python}"
CHIA="${MANTIS_LOOP_CHIA:-chia}"

# --dry-run needs no cluster; run it locally.
if [[ " $* " == *" --dry-run "* ]]; then
    exec "$PY" mantis_loop.py "$@"
fi

exec "$CHIA" job submit --working-dir . -- "$PY" mantis_loop.py "$@"
