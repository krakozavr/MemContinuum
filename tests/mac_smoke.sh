#!/usr/bin/env bash
# tests/mac_smoke.sh -- Anatomy M2b B7 portability smoke: a PARSER PROBE,
# nothing wider.
#
# Pgrep-guards the app process named in tests/mac_smoke.local (or the
# MC_MAC_SMOKE_GUARD_PROCESS env var) on the target host FIRST, before any
# command that could touch the machine runs -- abort with a named message
# if that app is open (the owner's standing rule: never build/test on the
# Mac mini while the guarded app is open). Only once that guard clears
# does this script go over SSH at all: it creates a disposable temp venv
# with the EXPLICIT login-shell python3 (never bare `python3` over a remote shell
# -- that resolves Apple's own /usr/bin/python3 3.9.6, which cannot
# install these wheels), installs the seven tree-sitter pins BY VERSION,
# copies this repo's own chunkers/ package over, and for one real fixture
# per language runs it through the SAME chunkers.get_chunker(lang)
# .chunk_file(...) entry point the local suite (tests/test_chunkers.py)
# uses -- asserting the identical (kind, qualified_name) identities that
# suite already golds for that exact file -- then deletes the temp venv.
#
# The seven pins are DERIVED from chunkers.LANGUAGE_TABLE (the same
# derivation tests/test_release.py's tree_sitter_pins() performs), never
# read off the repo's own hash-pinned lockfile: that file's hashes are for
# THIS machine's Linux wheels and would refuse to match a Mac wheel.
#
# NOT a substitute for the macOS arm64 CI job: that job installs the FULL
# hash-pinned lockfile and runs the whole unittest suite (fastembed/
# onnxruntime included). A green run of one says nothing about the other,
# and neither reading covers what the other checks -- see
# docs/INTERNALS.md's own paragraph on this script for the same point.
set -eu

HOST="macmini"
DRY_RUN=0
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# Resolve the guard process name -- what the pgrep guard below checks for
# on the target host -- BEFORE anything else runs, dry run included: this
# is the one required source of that name, and this repo's public tracked
# content must never name the actual app (see tests/test_repo_init.py's
# TestNoMachineIdentifyingContent). $MC_MAC_SMOKE_GUARD_PROCESS wins if
# set; otherwise the first line of $MC_MAC_SMOKE_GUARD_FILE (default
# tests/mac_smoke.local, untracked and gitignored) is used. Neither
# source set is a hard abort -- fail closed, no default name, no ssh.
GUARD_FILE="${MC_MAC_SMOKE_GUARD_FILE:-$REPO_ROOT/tests/mac_smoke.local}"
GUARD_PROCESS="${MC_MAC_SMOKE_GUARD_PROCESS:-}"
if [ -z "$GUARD_PROCESS" ] && [ -f "$GUARD_FILE" ]; then
    GUARD_PROCESS="$(head -n 1 "$GUARD_FILE")"
fi
if [ -z "$GUARD_PROCESS" ]; then
    echo "ERROR: guard process name is unset -- set MC_MAC_SMOKE_GUARD_PROCESS or create $GUARD_FILE (one line, the process name to pgrep-guard on the target host)" >&2
    exit 1
fi

MAC_PYTHON="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
LOCAL_PYTHON="${MEMCONTINUUM_PYTHON:-python3}"

# Derive the pin list from chunkers.LANGUAGE_TABLE -- the same source
# tests/test_release.py's tree_sitter_pins() derives from, reimplemented
# here (not imported: that function lives in a test module) because this
# is bash and the parse has to happen through a python -c call.
PINS="$(PYTHONPATH= "$LOCAL_PYTHON" -c '
import sys
sys.path.insert(0, sys.argv[1])
import chunkers
pins = {}
for row in chunkers.LANGUAGE_TABLE.values():
    if row["backend"] != "tree-sitter":
        continue
    pins["tree-sitter"] = row["runtime_pin"]
    pins[row["grammar_module"].replace("_", "-")] = row["grammar_pin"]
for name in sorted(pins):
    print("%s==%s" % (name, pins[name]))
' "$REPO_ROOT")"

if [ -z "$PINS" ]; then
    echo "ERROR: chunkers.LANGUAGE_TABLE has no tree-sitter row to derive pins from" >&2
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "dry run: would pgrep-guard $GUARD_PROCESS on $HOST, then install:"
    printf '%s\n' "$PINS"
    exit 0
fi

# pgrep-guard the resolved process name before any other ssh call touches the host.
if ssh -o ConnectTimeout=8 "$HOST" pgrep -x "$GUARD_PROCESS" >/dev/null 2>&1; then
    echo "ERROR: $GUARD_PROCESS is running on $HOST -- abort (never build/test while it's open)" >&2
    exit 1
fi

TMP_REMOTE="$(ssh "$HOST" mktemp -d)"
trap 'ssh "'"$HOST"'" rm -rf "'"$TMP_REMOTE"'" 2>/dev/null || true' EXIT

ssh "$HOST" "$MAC_PYTHON" -m venv "$TMP_REMOTE/v"
# shellcheck disable=SC2029
ssh "$HOST" "$TMP_REMOTE/v/bin/python" -m pip install --quiet $PINS

# Copy this repo's own chunkers/ package over (chunkers/__init__.py,
# chunkers/treesitter.py, chunkers/queries/*.scm -- stdlib + tree-sitter
# only, no other repo file) so the remote check runs the SAME production
# chunk_file() entry point the local suite runs, not a hand-rolled
# stand-in of it.
scp -q -r "$REPO_ROOT/chunkers" "$HOST:$TMP_REMOTE/chunkers"

RC=0
# All SEVEN LANGUAGE_TABLE tree-sitter rows (ruling 83: typescript and tsx
# are separate rows/fixture corpora). Every language's check is
# unconditional -- no per-language exemption (Lua included: B5's admission
# criteria call out the community-org grammar as needing the MOST real-probe
# scrutiny, not less).
#
# Each language's smoke fixture is the first file, in sorted order, under
# its Task 3-8 fixture corpus that is not one of this plan's own
# deliberately-broken fixtures (every one of those has "error" somewhere
# in its name -- adjacent_error.js, error_recovery.*, syntax_error.*,
# ErrorRecovery.java, SyntaxError.java -- or is the no-callable-content one,
# NoCallable.java / no_callable.*; -iname makes this case-insensitive so it
# catches Java's PascalCase fixture names too, which a case-sensitive
# `-name 'error_recovery*'`-style match would miss).
for lang in javascript typescript tsx java php rust lua; do
    fixture_dir="$REPO_ROOT/tests/fixtures/${lang}_corpus"
    fixture="$(find "$fixture_dir" -maxdepth 1 -type f \
        ! -iname '*error*' ! -iname 'no_callable*' ! -iname 'nocallable*' \
        | sort | head -1)"
    if [ -z "$fixture" ]; then
        echo "$lang: SKIP (no smoke fixture found under $fixture_dir)"
        continue
    fi
    remote_fixture="$TMP_REMOTE/$(basename "$fixture")"
    scp -q "$fixture" "$HOST:$remote_fixture"
    # $lang/$remote_fixture are passed as ssh remote-command arguments below
    # (safe: mktemp paths and this repo's fixture basenames carry no shell
    # metacharacters). The expected-identities table is NOT passed that way
    # -- ssh joins remote-command arguments with a bare space and hands the
    # result to the remote shell to re-parse, so a JSON value's brackets and
    # quotes would corrupt that command line. It is embedded directly in the
    # heredoc below instead, which travels over the ssh channel's stdin and
    # is never touched by that join/re-parse step.
    if ssh "$HOST" "$TMP_REMOTE/v/bin/python" - "$TMP_REMOTE" "$lang" "$remote_fixture" <<'PYEOF'
import os
import sys

chunkers_parent, lang, fixture = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, chunkers_parent)

import chunkers
from chunkers import treesitter

# The (kind, qualified_name) identities tests/test_chunkers.py already golds
# for each language's smoke fixture (Test*Extraction classes) -- one entry
# per language, matching the sorted-first non-error fixture mac_smoke.sh's
# own selection picks (see its header comment above the language loop).
EXPECTED = {
    "javascript": [("function", "default")],
    "typescript": [("function", "plain"), ("function", "arrowed"),
                    ("function", "overloaded"), ("function", "Util.helper"),
                    ("constructor", "Widget.constructor"), ("accessor", "Widget.value")],
    "tsx": [("function", "default")],
    "java": [("method", "Container.Greeter.greet"), ("method", "Container.Color.label"),
             ("method", "Container.addTwo")],
    "php": [("method", "Shape.describe"), ("method", "Suit.label")],
    "rust": [("method", "Widget.get"), ("method", "Handle.name"), ("method", "Widget.render")],
    "lua": [("function", "compute_total")],
}
expected = sorted(EXPECTED[lang])

treesitter.reset_cache()
with open(fixture, "r", encoding="utf-8") as f:
    text = f.read()
name = os.path.basename(fixture)

try:
    result = chunkers.get_chunker(lang).chunk_file(text, name)
except Exception as exc:
    print(f"{lang}: FAIL (chunk_file raised {type(exc).__name__}: {exc} on {fixture})")
    sys.exit(1)

got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
if result.status != "ok" or got != expected:
    print(f"{lang}: FAIL (status={result.status!r} chunks={got!r} expected={expected!r} on {fixture})")
    sys.exit(1)
print(f"{lang}: ok (parsed {fixture}, {len(result.chunks)} chunks match)")
PYEOF
    then
        :
    else
        RC=1
    fi
done

exit "$RC"
