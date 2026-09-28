#!/usr/bin/env bash
# Real self-hosted replacement: the shipped installer, launcher, and worker
# replace a running official release with another, driven only by the request
# the app writes into /data. Nothing here stands in for the helper.
#
#   sudo scripts/test-host-helper.sh <previous-sha> <target-sha> [<seed-sha>]
#
# With <seed-sha>, the helper is installed from that older checkout instead of
# this one, so the target image's newer worker must arrive by itself: it is
# offered as the candidate, runs the next request, and becomes active.
#
# Both SHAs must have published official images. Run on a disposable systemd
# host with Docker Compose (a CI runner); it installs root-owned units there.
#
# 1. Start <previous> exactly as an owner deploys it: docker-compose.yml with an
#    absolute --env-file, project "mobius".
# 2. Install the launcher and seed the worker with scripts/install-rebuild-helper.sh.
# 3. Queue a version 2 request from inside the app, as Settings does.
# 4. Let the real mobius-rebuild.path/service run the launcher and worker.
# 5. Require the exact target image, a nonce-bound success, the worker and
#    launcher revisions, and an adoption outcome; then a second request for the
#    same image must report no_change.

set -euo pipefail

PREVIOUS="${1:?previous sha}"
TARGET="${2:?target sha}"
SEED="${3:-}"
IMAGE=ghcr.io/mobius-os/mobius
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
STATUS=/var/lib/mobius-rebuild/status.json
ENV_FILE=$(mktemp /tmp/mobius-host-helper.XXXXXX.env)
export COMPOSE_PROJECT_NAME=mobius

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

fail() {
  echo "host helper: $*" >&2
  systemctl --no-pager status mobius-rebuild.service mobius-rebuild.path >&2 || true
  journalctl --no-pager -u mobius-rebuild.service -n 80 >&2 || true
  cat "$STATUS" >&2 2>/dev/null || true
  docker logs mobius --tail 60 >&2 2>&1 || true
  exit 1
}

field() {  # <json-file> <python expression over d>
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}

wait_status() {  # <nonce> <state>...: wait until the root status names this request
  local nonce=$1; shift
  for _ in $(seq 1 240); do
    if [[ -f $STATUS ]] && [[ $(field "$STATUS" 'd.get("request_nonce")') == "$nonce" ]]; then
      local state; state=$(field "$STATUS" 'd.get("state")')
      for wanted in "$@"; do [[ $state == "$wanted" ]] && return 0; done
      case "$state" in failed|rolled_back|needs_recovery) fail "request ended $state";; esac
    fi
    sleep 5
  done
  fail "request $nonce did not reach $* within 20 minutes"
}

queue() {  # <sha>: write the request exactly as the app does; prints its nonce
  local nonce; nonce=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  docker exec -u mobius mobius sh -c "
    printf '%s' '{\"version\":2,\"expected_sha\":\"$1\",\"nonce\":\"$nonce\"}' \
      > /data/mobius-rebuild/inbox/.request.tmp &&
    mv /data/mobius-rebuild/inbox/.request.tmp /data/mobius-rebuild/inbox/request.json" \
    || fail "the app could not queue a request"
  echo "$nonce"
}

echo "host helper: $IMAGE:sha-${PREVIOUS:0:12} -> sha-${TARGET:0:12}"
printf 'SECRET_KEY=host-helper-regression-key-0123456789abcdef\nDOMAIN=localhost\n' >"$ENV_FILE"
chmod 0600 "$ENV_FILE"

# The checkout the owner deploys from and installs the helper with.
INSTALL_ROOT=$ROOT
if [[ -n $SEED ]]; then
  INSTALL_ROOT=$(mktemp -d /tmp/mobius-seed.XXXXXX)
  git -C "$ROOT" worktree add --detach -q "$INSTALL_ROOT/checkout" "$SEED"
  INSTALL_ROOT=$INSTALL_ROOT/checkout
  if [[ ! -f $INSTALL_ROOT/scripts/mobius-rebuild-launcher.py ]]; then
    echo "host helper: ${SEED:0:12} predates the launcher; nothing to prove yet"
    exit 0
  fi
fi

echo "1. the previous release runs as an owner deploys it"
cd "$INSTALL_ROOT"
MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" docker compose --env-file "$ENV_FILE" \
  up -d --no-build --no-deps app
for _ in $(seq 1 60); do
  [[ $(docker inspect -f '{{.State.Health.Status}}' mobius 2>/dev/null) == healthy ]] && break
  sleep 5
done
[[ $(docker inspect -f '{{.State.Health.Status}}' mobius) == healthy ]] \
  || fail "the previous release did not become healthy"

echo "2. the owner installs the helper once"
# Compose labels name this checkout; the installer freezes that topology.
scripts/install-rebuild-helper.sh || fail "the installer failed"
[[ $(field "$STATUS" 'd.get("launcher_revision")') == 1 ]] \
  || fail "the installed helper is not the launcher"
revision=$(field "$STATUS" 'd.get("worker_revision")')
latest() { python3 -c 'import json, sys; print(max(map(int, json.load(open(sys.argv[1])))))' "$1/scripts/rebuild-worker-revisions.json"; }
[[ $revision == $(latest "$INSTALL_ROOT") ]] \
  || fail "the seeded worker is not the installing checkout's revision"

echo "3. Settings queues an update to the target release"
nonce=$(queue "$TARGET")

echo "4. the real path unit runs the launcher and worker"
wait_status "$nonce" succeeded
running=$(docker inspect -f '{{.Image}}' mobius)
[[ $running == $(docker image inspect -f '{{.Id}}' "$IMAGE:sha-$TARGET") ]] \
  || fail "the running container is not the target image"
[[ $(docker exec mobius curl -fsS http://127.0.0.1:8000/api/version \
      | python3 -c 'import json, sys; print(json.load(sys.stdin).get("sha"))') == "$TARGET" ]] \
  || fail "the container does not serve the target revision"
[[ $(field "$STATUS" 'd.get("worker_revision")') == "$revision" ]] \
  || fail "a different worker reported the replacement"
adoption=$(field "$STATUS" 'd.get("worker_adoption") or ""')
[[ -n $adoption && $adoption != rejected* ]] || fail "unexpected worker adoption: $adoption"
echo "   worker adoption: $adoption"

echo "5. the same request again changes nothing"
nonce=$(queue "$TARGET")
wait_status "$nonce" no_change
if [[ -n $SEED ]]; then
  target_revision=$(latest "$ROOT")
  # The worker reports before it exits; the launcher then settles the
  # candidate. Wait for the service to finish and the record to name it.
  active_revision() { python3 -c 'import json; print(json.load(open("/var/lib/mobius-rebuild/workers.json"))["active"]["revision"])'; }
  for _ in $(seq 1 60); do
    if ! systemctl is-active --quiet mobius-rebuild.service \
       && [[ $(active_revision) == "$target_revision" ]]; then break; fi
    sleep 2
  done
  active=$(active_revision)
  [[ $active == "$target_revision" ]] \
    || fail "the image's worker revision $target_revision did not become active (active: $active; adoption: $adoption)"
  [[ $(field "$STATUS" 'd.get("worker_revision")') == "$target_revision" ]] \
    || fail "the second replacement was not run by the adopted worker"
  echo "   worker revision $revision -> $target_revision arrived with the image and is active"
fi
echo "host helper: sha-${PREVIOUS:0:12} replaced by sha-${TARGET:0:12} through the installed launcher"
