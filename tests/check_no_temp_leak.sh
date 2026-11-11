#!/usr/bin/env bash
# tests/check_no_temp_leak.sh -- run a command and fail if it leaves new
# entries in the system temp directory.
#
# Usage: tests/check_no_temp_leak.sh COMMAND [ARGS...]
#   e.g. tests/check_no_temp_leak.sh python -m unittest discover -s tests
#        tests/check_no_temp_leak.sh bash tests/run_bash32.sh
#
# Snapshots the entries of ${TMPDIR:-/tmp} that this user owns and whose name
# starts with "memcontinuum-" or "tmp" (the project's own prefix and the
# tempfile / mktemp default), runs the command, snapshots again, and lists
# every entry present only in the second snapshot. Entries that existed before
# the run are never touched or reported. Exit status: the command's own status
# if it failed; else 70 when a snapshot or the comparison itself failed; else 1 when
# new entries remain; else 0.
#
# Note: another process creating a matching entry while the command runs is
# reported too -- rerun on a quiet machine before suspecting the command.
# bash 3.2 compatible (no mapfile, no associative arrays).

set -u

if [ "$#" -eq 0 ]; then
    echo "usage: $0 COMMAND [ARGS...]" >&2
    exit 64
fi

TMP_ROOT="${TMPDIR:-/tmp}"
ME="$(id -un)"
WORK="$(mktemp -d "${TMP_ROOT%/}/mc-leakcheck.XXXXXX")" || exit 70

# find's exit status is checked: at -maxdepth 1 it only stats the entries of the
# root (it does not descend), so entries of other users do not make it fail. The
# one benign failure is an entry another process removes between find's
# directory read and its stat; one retry absorbs that race, and a second failure
# is a failed snapshot. The root itself is checked first: it must be a readable,
# searchable directory. The raw list is sorted into the snapshot file, and the
# sort's status is checked too.
snapshot() {
    [ -d "$TMP_ROOT" ] && [ -r "$TMP_ROOT" ] && [ -x "$TMP_ROOT" ] || return 1
    if ! find "$TMP_ROOT" -maxdepth 1 -mindepth 1 -user "$ME" \
        \( -name 'memcontinuum-*' -o -name 'tmp*' \) -print >"$1.raw" 2>/dev/null; then
        find "$TMP_ROOT" -maxdepth 1 -mindepth 1 -user "$ME" \
            \( -name 'memcontinuum-*' -o -name 'tmp*' \) -print >"$1.raw" 2>/dev/null || return 1
    fi
    LC_ALL=C sort "$1.raw" >"$1" || return 1
    [ -f "$1" ]
}

snapshot_failed() {
    echo "check_no_temp_leak: could not snapshot $TMP_ROOT" >&2
    rm -rf "$WORK"
    exit 70
}

snapshot "$WORK/before" || snapshot_failed
"$@"
RC=$?
if ! snapshot "$WORK/after"; then
    if [ "$RC" -ne 0 ]; then rm -rf "$WORK"; exit "$RC"; fi
    snapshot_failed
fi
if LC_ALL=C comm -13 "$WORK/before" "$WORK/after" >"$WORK/new"; then
    if ! NEW="$(cat "$WORK/new")"; then
        if [ "$RC" -ne 0 ]; then rm -rf "$WORK"; exit "$RC"; fi
        echo "check_no_temp_leak: could not read the new-entries list" >&2
        rm -rf "$WORK"
        exit 70
    fi
else
    if [ "$RC" -ne 0 ]; then rm -rf "$WORK"; exit "$RC"; fi
    echo "check_no_temp_leak: could not compare the temp snapshots" >&2
    rm -rf "$WORK"
    exit 70
fi
rm -rf "$WORK"

if [ "$RC" -ne 0 ]; then
    exit "$RC"
fi
if [ -n "$NEW" ]; then
    echo "check_no_temp_leak: the command left new entries in $TMP_ROOT:" >&2
    printf '%s\n' "$NEW" | sed 's/^/  /' >&2
    exit 1
fi
exit 0
