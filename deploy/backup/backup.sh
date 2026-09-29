#!/bin/sh
# Nightly PostgreSQL backup.  Usage:  backup.sh once | loop | verify FILE
#
#  - custom-format dump (pg_dump -Fc), written to a temp name and moved into place only after
#    `pg_restore --list` can read it, so a half-written dump is never mistaken for a backup
#  - keeps the newest BACKUP_KEEP files BY COUNT (a clock jump must not wipe the folder)
#  - dumps contain the full text of every indexed document plus the audit and Q&A logs:
#    protect the folder like the NAS itself
set -eu
umask 077  # dumps hold every document's text: readable by the owner only

DIR="${BACKUP_DIR_IN_CONTAINER:-/backups}"
KEEP="${BACKUP_KEEP:-14}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') backup: $*"; }

verify() {
    pg_restore --list "$1" > /dev/null
}

prune() {
    # newest first by NAME (names carry a sortable UTC timestamp), delete everything after KEEP
    ls -1 "$DIR"/rug-*.dump 2>/dev/null | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
        log "removing old backup $old"
        rm -f -- "$old"
    done
}

once() {
    mkdir -p "$DIR"
    name="rug-$(date -u '+%Y%m%dT%H%M%SZ').dump"
    tmp="$DIR/.$name.part"
    trap 'rm -f -- "$tmp"' EXIT
    pg_dump --format=custom --no-owner --file="$tmp"
    verify "$tmp"
    mv -- "$tmp" "$DIR/$name"
    trap - EXIT
    log "wrote $DIR/$name ($(wc -c < "$DIR/$name") bytes)"
    prune
}

seconds_until() {  # local HH:MM -> seconds from now (today, or tomorrow if already past)
    now=$(date '+%s')
    target=$(date -d "today $1" '+%s' 2>/dev/null || date -d "$1" '+%s')
    [ "$target" -le "$now" ] && target=$((target + 86400))
    echo $((target - now))
}

case "${1:-once}" in
    once) once ;;
    verify) verify "$2" && log "$2 is readable" ;;
    loop)
        at="${BACKUP_AT:-02:30}"
        log "scheduled daily at $at (keeping $KEEP)"
        while true; do
            sleep "$(seconds_until "$at")"
            once || log "FAILED: see the message above"
            sleep 61  # never run twice in the same minute
        done
        ;;
    *) echo "usage: $0 once|loop|verify FILE" >&2; exit 2 ;;
esac
