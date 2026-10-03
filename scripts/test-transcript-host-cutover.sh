#!/usr/bin/env bash
# Read-only safety/image preflight for the private disposable-host recipe.
# Deliberately does not install units or replace containers: the production
# worker accepts only pulled official amd64 SHA images, not a local candidate.
set -euo pipefail

usage() { echo "usage: $0 <candidate-full-sha> <local-candidate-image>" >&2; exit 2; }
[[ $# == 2 ]] || usage
sha=$1 image=$2
[[ $sha =~ ^[0-9a-f]{40}$ && $image != *[[:space:]]* ]] || usage
command -v docker >/dev/null || { echo "Docker unavailable" >&2; exit 2; }
command -v systemctl >/dev/null || { echo "systemd unavailable" >&2; exit 2; }
[[ -d /run/systemd/system ]] || { echo "not a systemd host" >&2; exit 2; }
docker info >/dev/null

# Production names and paths are used by the real installer. Refuse to even
# prepare a recipe on a host with any prior installation; never attach live data.
for name in mobius; do
  if docker container inspect "$name" >/dev/null 2>&1; then
    echo "existing container $name: fresh disposable host required" >&2; exit 2
  fi
done
if docker volume inspect mobius_app_data >/dev/null 2>&1; then
  echo "existing mobius_app_data volume: fresh disposable host required" >&2; exit 2
fi
for path in /etc/mobius-rebuild /var/lib/mobius-rebuild \
  /usr/local/libexec/mobius-rebuild-host \
  /etc/systemd/system/mobius-rebuild.service \
  /etc/systemd/system/mobius-rebuild.path \
  /etc/systemd/system/mobius-rebuild-reconcile.service; do
  if [[ -e $path || -L $path ]]; then
    echo "existing helper path $path: fresh disposable host required" >&2; exit 2
  fi
done

revision=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
source=$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.source"}}' "$image")
arch=$(docker image inspect -f '{{.Architecture}}' "$image")
[[ $revision == "$sha" ]] || { echo "candidate revision label mismatch" >&2; exit 2; }
[[ $source == https://github.com/mobius-os/mobius ]] \
  || { echo "candidate source label mismatch" >&2; exit 2; }
[[ $arch == amd64 || $arch == arm64 ]] \
  || { echo "unsupported candidate architecture: $arch" >&2; exit 2; }
printf 'Fresh disposable host and local candidate verified: sha=%s arch=%s image=%s\n' "$sha" "$arch" "$image"
printf 'READ-ONLY PREFLIGHT ONLY. No replacement or rollback proof has run.\n'
