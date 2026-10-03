#!/usr/bin/env bash
set -euo pipefail
if [[ ${HERMES_HOME:-/home/ubuntu/.hermes} != /home/ubuntu/.hermes ]]; then
  printf '%s\n' 'Foreign HERMES_HOME refused' >&2
  exit 1
fi
export HERMES_HOME=/home/ubuntu/.hermes
export PYTHONPATH=/home/ubuntu/hermes-agent-upstream
out="$HERMES_HOME/state/alibaba-safe-candidates"
mkdir -p "$out"
exec 9>"$out/refresher.lock"
flock -n 9 || exit 0
stamp=$(date -u +%Y%m%dT%H%M%SZ)
exec /home/ubuntu/hermes-agent-upstream/venv/bin/python "$HERMES_HOME/scripts/hypest_alibaba_safe.py" --candidate "$out/$stamp-candidate.json" --evidence "$out/$stamp-evidence.json"
