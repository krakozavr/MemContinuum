#!/usr/bin/env bash
# mc-list-lib.sh -- shared, pure-bash list helper. Sourced by
# memcontinuum-decide.sh and memcontinuum-update.sh -- never executed
# standalone.
#
# Why this is its own file and not part of mc-registry-lib.sh: neither the
# session-start detector nor memcontinuum-state.sh needs a list join, and
# mc-registry-lib.sh is sourced by the detector on every session start in
# every repo on the machine -- no unrelated function belongs there.
#
# Side-effect-free at SOURCE time: this file only defines a function. Safe to
# `.` under `set -u`. Bash 3.2 compatible (no associative arrays, no
# namerefs, no `${arr[*]/%/;}` tricks, no external tr/paste dependency).

# mc_join_semi ELEMENT... -- prints the arguments joined by ';' with no
# trailing separator, and nothing at all for zero arguments. Elements are
# joined RAW: empty elements are kept (they leave adjacent separators),
# spaces and a literal '%' pass through untouched, and every element goes
# through printf's '%s', never its format string. A caller that stores the
# result in a percent-encoded column encodes each element BEFORE joining
# (memcontinuum-decide.sh does, with mc_note_encode); this function never
# encodes, so encoding happens exactly once.
mc_join_semi() {
    local out="" first=1 a
    for a in "$@"; do
        if [ "$first" -eq 1 ]; then out="$a"; first=0; else out="$out;$a"; fi
    done
    printf '%s' "$out"
}
