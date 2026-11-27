#!/bin/zsh
# usage: ./release.sh <pr-number>
# Wait for the pull request's CI, squash-merge it, wait for the image built
# from the merged commit, pin that image on the NAS, recreate the containers
# and remove the older images.
#
# Settings (environment, all optional):
#   JACKERY_NAS_HOST     ssh host of the NAS                  (default: nas)
#   JACKERY_COMPOSE_DIR  folder holding docker-compose.yml    (default: /volume1/docker/jackery-monitor)
#   JACKERY_DOCKER       docker binary on the NAS             (default: Synology Container Manager's)
set -e

# Everything runs inside a function so the shell has read the whole file
# before `git pull` can replace it mid-run.
main() {
  cd "${0:A:h}"
  local PR=$1
  [ -n "$PR" ] || { echo "usage: $0 <pr-number>"; exit 2; }
  local NAS=${JACKERY_NAS_HOST:-nas}
  local DIR=${JACKERY_COMPOSE_DIR:-/volume1/docker/jackery-monitor}
  local D=${JACKERY_DOCKER:-/var/packages/ContainerManager/target/usr/bin/docker}
  local IMG=ghcr.io/$(gh repo view --json nameWithOwner --jq .nameWithOwner | tr '[:upper:]' '[:lower:]')
  local NAME=${IMG:t}
  local R SHA S7 DIG i

  sleep 20        # give CI a moment to register its checks
  for i in $(seq 1 40); do
    R=$(gh pr checks $PR 2>&1 || true)
    if echo "$R" | grep -qE 'pending|queued|in_progress|no checks'; then sleep 12; else break; fi
  done
  if echo "$R" | grep -q fail; then echo "CI FAILED"; echo "$R"; exit 1; fi
  gh pr merge $PR --squash --delete-branch >/dev/null 2>&1 || true
  git checkout -q main; git pull -q --ff-only; SHA=$(git rev-parse HEAD); S7=${SHA:0:7}
  gh pr view $PR --json state,mergeCommit --jq '"PR \(.state) \(.mergeCommit.oid[0:7])"'

  for i in $(seq 1 50); do
    R=$(gh run list --commit $SHA --json name,status,conclusion --jq '.[] | "\(.name)=\(.status)/\(.conclusion)"' | tr '\n' ' ')
    case "$R" in *in_progress*|*queued*|*pending*|"") sleep 15;; *) break;; esac
  done
  echo "$R"; case "$R" in *failure*|"") echo "BUILD FAILED"; exit 1;; esac

  DIG=$(ssh $NAS "sudo $D pull -q $IMG:main-$S7 >/dev/null && sudo $D inspect --format '{{index .RepoDigests 0}}' $IMG:main-$S7" 2>/dev/null | sed 's/.*@//')
  [ -n "$DIG" ] || { echo "NO DIGEST"; exit 1; }
  ssh $NAS "cd $DIR && cp docker-compose.yml docker-compose.yml.bak.\$(date +%Y%m%d-%H%M%S) && sed -i -E 's|$NAME:main-[0-9a-f]{7}@sha256:[0-9a-f]{64}|$NAME:main-$S7@$DIG|' docker-compose.yml && grep -c 'main-$S7@$DIG' docker-compose.yml && sudo $D compose up -d >/dev/null 2>&1; sleep 25; sudo $D ps --filter name=jackery --format '{{.Names}} {{.Image}} {{.Status}}' | cut -c1-110" 2>/dev/null

  # Each release leaves a dated copy of docker-compose.yml; keep the newest five.
  ssh $NAS "cd $DIR && ls -1t docker-compose.yml.bak.* 2>/dev/null | tail -n +6 | while read f; do rm -f \"\$f\" 2>/dev/null; done; echo \"compose backups kept: \$(ls -1 docker-compose.yml.bak.* 2>/dev/null | wc -l)\"" 2>/dev/null

  # Once both containers are up on the new image, remove the older images of
  # this repository so they do not pile up. Docker itself refuses to remove
  # one a container still uses.
  ssh $NAS "UP=\$(sudo $D ps --filter name=jackery --filter status=running --format '{{.Image}}' | grep -c 'main-$S7'); if [ \"\$UP\" = 2 ]; then sudo $D images $IMG --format '{{.ID}} {{.Tag}}' | grep -v ' main-$S7\$' | cut -d' ' -f1 | sort -u | while read id; do sudo $D rmi \$id >/dev/null 2>&1 && echo \"removed old image \$id\"; done; echo \"Jackery images on the NAS: \$(sudo $D images $IMG -q | sort -u | wc -l)\"; else echo 'new containers not both running: old images kept'; fi" 2>/dev/null
}

main "$@"
