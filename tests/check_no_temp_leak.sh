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
# if it failed; else 1 when new entries remain; else 0.
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

snapshot() {
    find "$TMP_ROOT" -maxdepth 1 -mindepth 1 -user "$ME" \
        \( -name 'memcontinuum-*' -o -name 'tmp*' \) -print 2>/dev/null | LC_ALL=C sort
}

snapshot >"$WORK/before"
"$@"
RC=$?
snapshot >"$WORK/after"
LC_ALL=C comm -13 "$WORK/before" "$WORK/after" >"$WORK/new"
NEW="$(cat "$WORK/new")"
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
