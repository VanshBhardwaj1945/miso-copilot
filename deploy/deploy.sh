#!/usr/bin/env bash
# Bring this host to whatever origin/main is. Safe to run unattended.
#
# Exits early when main has not moved, so the common case costs one git fetch.
# A failed frontend build leaves the previous bundle serving rather than an
# empty directory: the site staying one commit old beats the site going blank.
set -uo pipefail

APP=/opt/miso/app
WEB=/opt/miso/web
LOCK=/opt/miso/deploy.lock
BRANCH=main

exec 9>"$LOCK"
# a deploy can outlast the timer interval; never run two at once
flock -n 9 || { echo "$(date -Is) another deploy is running, skipping"; exit 0; }

cd "$APP"
git fetch -q origin "$BRANCH" || { echo "$(date -Is) fetch failed"; exit 1; }
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse FETCH_HEAD)
[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "$(date -Is) deploying ${LOCAL:0:8} -> ${REMOTE:0:8}"
# A tracked file the app rewrites - data/logs/requests.jsonl was one - makes
# this abort, and without the guard the script went on to rebuild, restart and
# report healthy while the code never moved. The host silently stopped
# deploying and nothing said so.
if ! git checkout -q -B deploy FETCH_HEAD; then
  echo "$(date -Is) checkout FAILED - host is NOT on ${REMOTE:0:8}"
  git status --short | head
  exit 1
fi

# --- frontend: build into a temp dir, swap only on success ---
if [ -f frontend/package.json ]; then
  ( cd frontend && npm ci --silent && npm run build --silent ) \
    && [ -d frontend/dist ] \
    && { sudo rm -rf "$WEB"; sudo mkdir -p "$WEB"; sudo cp -r frontend/dist/* "$WEB"/; \
         echo "$(date -Is) frontend rebuilt"; } \
    || echo "$(date -Is) frontend build FAILED - keeping the bundle already serving"
fi

# --- backend + corpus ---
# Chroma has one writer: the backend must be down before ingest touches it.
sudo systemctl stop miso-backend
# Whatever happens between here and the start below, the backend comes back.
# A deploy that died in the middle left the host with no service at all, and
# the next run exits early because the commit already matches - so nothing
# ever started it again.
trap 'sudo systemctl start miso-backend || true' EXIT
source .venv/bin/activate
pip install -q -r requirements.txt 2>&1 | tail -1
# The poller exits 2 when the rate guard skips every endpoint, which is normal
# right after a scheduled cycle and is not a deploy failure. Only 1 is a real
# fetch failure, and even then the payloads already on disk are what the
# corpus gets rebuilt from.
python -m backend.poller --once 2>&1 | tail -1
poll_status=${PIPESTATUS[0]}
if [ "$poll_status" = "1" ]; then
  echo "$(date -Is) poll failed, rebuilding from the payloads on disk"
fi
python -m backend.rag.ingest_docs 2>&1 | tail -1
trap - EXIT
sudo systemctl start miso-backend

# Poll rather than sleep once. Boot loads the embedding model, which takes
# about twenty seconds on this box, so a flat 12 called every good deploy a
# failure - and an alarm that cries wolf is how a real one gets ignored.
echo "$(date -Is) waiting for health"
for _ in $(seq 1 30); do
  curl -fsS --max-time 5 http://127.0.0.1:8000/health >/dev/null 2>&1 && break
  sleep 3
done
if curl -fsS --max-time 10 http://127.0.0.1:8000/health >/dev/null; then
  echo "$(date -Is) deployed ${REMOTE:0:8} - healthy"
else
  echo "$(date -Is) deployed ${REMOTE:0:8} - HEALTH CHECK FAILED"
  exit 1
fi
