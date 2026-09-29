#!/bin/sh
# Restore a backup into a database.  Usage:  restore.sh FILE.dump TARGET_DB [--replace]
#
# By default TARGET_DB must not exist (it is created; the dump itself creates the pgvector extension). With
# --replace an existing TARGET_DB is dropped first. Uses the PG* environment variables.
# To restore in place: stop app, worker and backup, run this with --replace against "rug",
# start them again. The catalog and embeddings can also be rebuilt from the NAS with
# `rug ingest` (slow), so a restore mostly saves that time and brings back users' history,
# permissions, the Q&A log and the audit log, which the NAS cannot.
set -eu
FILE="$1"; DB="$2"; MODE="${3:-}"
pg_restore --list "$FILE" > /dev/null
exists=$(psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$DB'")
if [ -n "$exists" ]; then
    [ "$MODE" = "--replace" ] || { echo "database $DB exists; pass --replace to drop it" >&2; exit 1; }
    psql -d postgres -qc "DROP DATABASE \"$DB\" WITH (FORCE)"
fi
psql -d postgres -qc "CREATE DATABASE \"$DB\""
pg_restore --no-owner --dbname="$DB" "$FILE"
echo "restored $FILE into $DB"
