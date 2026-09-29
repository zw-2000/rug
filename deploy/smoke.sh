#!/bin/sh
# End-to-end smoke test of the deployment files on THIS host.
#
#   deploy/smoke.sh                 build, start, check, tear down
#   KEEP=1 deploy/smoke.sh          leave the stack running afterwards
#   RUG_BUILD_ARGS="--network host --secret id=ca,src=/path/ca.pem" deploy/smoke.sh
#                                   extra `docker build` arguments (e.g. behind a TLS proxy)
#
# What it runs: the real Caddy and Postgres containers and the real image, but the app started
# from the `smoke` target: a MOCK directory, FAKE models and a synthetic corpus. So it proves
# the deployment wiring (TLS, proxying and streaming, cookies, client addresses, checksums,
# backup and restore), NOT production sign-in against Active Directory, Ollama or the GPU.
set -eu
cd "$(dirname "$0")"

PROJECT=rugsmoke
COMPOSE="docker compose -p $PROJECT -f docker-compose.yml -f docker-compose.smoke.yml --env-file smoke.env"
SOW="/tmp/rug-smoke-docs/sales/Boost Connect SR-1098 SOW v2 FINAL.docx"
status=0

cleanup() {
    if [ "${KEEP:-0}" != "1" ]; then
        $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf smoke.env smoke-ca.crt backups-smoke
    fi
}
fail() { echo "FAIL $*"; status=1; }
pass() { echo "PASS $*"; }
trap cleanup EXIT

umask 077
printf 'POSTGRES_PASSWORD=%s\nDOCS_DIR=/tmp\nRUG_HOST=localhost\nBACKUP_DIR=./backups-smoke\n' \
    "$(python3 -c 'import secrets; print(secrets.token_hex(16))')" > smoke.env
mkdir -p backups-smoke

echo "== build =="
# shellcheck disable=SC2086
docker build ${RUG_BUILD_ARGS:-} --target smoke -f Dockerfile -t rug-app:local .. > /tmp/rug-smoke-build.log 2>&1 \
    || { tail -30 /tmp/rug-smoke-build.log; exit 1; }

echo "== start =="
$COMPOSE up -d --no-build --wait --wait-timeout 240 || { $COMPOSE logs --tail 40; exit 1; }

echo "== caddy's root certificate =="
for _ in $(seq 1 30); do
    $COMPOSE cp caddy:/data/caddy/pki/authorities/local/root.crt ./smoke-ca.crt >/dev/null 2>&1 && break
    sleep 1
done
[ -s smoke-ca.crt ] || { $COMPOSE logs caddy --tail 30; exit 1; }

echo "== HTTPS checks =="
sha=$($COMPOSE exec -T app sha256sum "$SOW" | cut -d' ' -f1)
python3 smoke_check.py --host localhost --ca smoke-ca.crt --expect-sha "$sha" || status=1

echo "== container hardening =="
[ "$($COMPOSE exec -T app id -u)" = "10001" ] && pass "app runs as a non-root user" || fail "app runs as root"
ports=$(docker ps --filter "label=com.docker.compose.project=$PROJECT" --format '{{.Names}} {{.Ports}}' | grep -E '0\.0\.0\.0:[0-9]+' | grep -v caddy || true)
[ -z "$ports" ] && pass "only Caddy publishes ports" || fail "other services publish ports: $ports"

echo "== backup and restore =="
$COMPOSE exec -T backup sh /backup.sh once
newest=$(ls -1 backups-smoke/rug-*.dump | sort | tail -1)
name=$(basename "$newest")
# the exact form the README documents for operators
$COMPOSE run --rm --no-deps -T --entrypoint sh backup /restore.sh "/backups/$name" rug_restore_check --replace
count() { $COMPOSE exec -T backup psql -d "$1" -Atc "SELECT count(*) FROM $2" | tr -d '\r'; }
for t in documents chunks audit_log users; do
    a=$(count rug_e2e $t); b=$(count rug_restore_check $t)
    if [ -n "$a" ] && [ "$a" = "$b" ] && [ "$a" -gt 0 ]; then pass "restored $t = $a rows"; else fail "restored $t: source=$a restored=$b"; fi
done
ext=$($COMPOSE exec -T backup psql -d rug_restore_check -Atc "SELECT extname FROM pg_extension WHERE extname='vector'" | tr -d '\r')
[ "$ext" = "vector" ] && pass "pgvector extension restored" || fail "pgvector extension missing after restore"
vec=$($COMPOSE exec -T backup psql -d rug_restore_check -Atc "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL" | tr -d '\r')
[ "${vec:-0}" -gt 0 ] && pass "embeddings survived the restore ($vec)" || fail "no embeddings after restore"
$COMPOSE exec -T backup psql -d postgres -qc 'DROP DATABASE rug_restore_check WITH (FORCE)'
for _ in 1 2 3; do sleep 1; $COMPOSE exec -T -e BACKUP_KEEP=2 backup sh /backup.sh once >/dev/null; done
kept=$(ls -1 backups-smoke/rug-*.dump | wc -l)
[ "$kept" = "2" ] && pass "retention keeps the newest 2 by count" || fail "retention left $kept files"
mode=$(stat -c '%a' "$(ls -1 backups-smoke/rug-*.dump | head -1)")
[ "$mode" = "600" ] && pass "dump files are owner-only (mode $mode)" || fail "dump files have mode $mode"
leftover=$(ls -a backups-smoke | grep -c '\.part' || true)
[ "$leftover" = "0" ] && pass "no partial dump files left behind" || fail "partial dump files present"

[ "$status" = "0" ] && echo "SMOKE PASSED" || { echo "SMOKE FAILED"; $COMPOSE logs --tail 30 app caddy; }
exit "$status"
