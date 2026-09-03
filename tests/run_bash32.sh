#!/usr/bin/env bash
# tests/run_bash32.sh -- macOS-port verification harness (docs/DESIGN.md SS8
# port note, 2026-08-30). Re-runs the shell-driving test suites with every
# subprocess invoked under a REAL
# bash 3.2 binary instead of whatever "bash" resolves to on this machine --
# stock macOS still ships bash 3.2.57 today, and it is the actual bar the
# hooks/*.sh port targets, not `bash --posix` under a modern bash (which
# does not catch bash-4/5-isms like `mapfile`, `${var,,}`, or
# $EPOCHREALTIME -- those are simply unavailable in --posix mode's own
# vocabulary, not rejected the way an old interpreter rejects them).
#
# Usage:
#   tests/run_bash32.sh                 # builds bash 3.2.57 into
#                                        # ~/.cache/bash32 if not already
#                                        # there, then runs the suites
#                                        # under it.
#   tests/run_bash32.sh --build-only    # only (re)build bash 3.2.57.
#   MC_BASH32=/path/to/bash tests/run_bash32.sh
#                                        # skip the build, use this bash
#                                        # binary instead (e.g. a real Mac's
#                                        # /bin/bash over ssh).
#
# Building bash 3.2.57 needs a C compiler (clang or gcc), plus make, bison
# (or another yacc), and m4 -- typically already on a dev machine. Network
# access is required only for the one-time source download. Nothing here
# ever touches system-wide install locations; everything lands under
# ~/.cache/bash32(-build).
#
# Env passed through to the test suites, same as running them normally:
#   MEMCONTINUUM_PYTHON must be set to a venv python (see README.md
#   "Requirements"); PYTHONPATH is cleared before invoking python, same as
#   every hook's own defensive hard-clear.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"

BASH32_VERSION="3.2.57"
BUILD_ROOT="${MC_BASH32_BUILD_ROOT:-$HOME/.cache/bash32-build}"
INSTALL_ROOT="${MC_BASH32_INSTALL_ROOT:-$HOME/.cache/bash32}"
BASH32_BIN="$INSTALL_ROOT/bin/bash"

log() { printf '%s\n' "$1"; }

build_bash32() {
    if [ -x "$BASH32_BIN" ]; then
        log "== bash $BASH32_VERSION already built at $BASH32_BIN =="
        return 0
    fi

    log "== building bash $BASH32_VERSION into $INSTALL_ROOT (one-time) =="
    mkdir -p "$BUILD_ROOT/src" "$BUILD_ROOT/debs" "$BUILD_ROOT/extracted" "$INSTALL_ROOT/bin"

    # A bare CI runner's apt package lists may be empty or stale -- without
    # this refresh, the fetch below fails silently (its own `|| true`
    # swallows the error) and the toolchain never lands, so the build fails
    # later with a much less obvious "make: command not found". Best-effort:
    # a runner with a pre-populated apt cache, or no `apt-get` at all on a
    # non-Debian host, must not fail the build over this.
    apt-get update >/dev/null 2>&1 || true

    # Fetch a minimal, self-contained build toolchain (make/bison/m4) via
    # `apt-get download` + `dpkg-deb --extract` -- neither needs root, so
    # this works even on a machine with no system compiler toolchain
    # installed and no sudo. A real C compiler (clang or gcc) is still
    # assumed to already be on PATH.
    local ext="$BUILD_ROOT/extracted"
    export PATH="$ext/usr/bin:$PATH"
    if ! command -v make >/dev/null 2>&1; then
        (cd "$BUILD_ROOT/debs" && apt-get download make >/dev/null 2>&1) || true
        for f in "$BUILD_ROOT"/debs/make_*.deb; do
            [ -f "$f" ] && dpkg-deb --extract "$f" "$ext"
        done
    fi
    if ! command -v bison >/dev/null 2>&1 && ! command -v yacc >/dev/null 2>&1; then
        (cd "$BUILD_ROOT/debs" && apt-get download bison m4 >/dev/null 2>&1) || true
        for f in "$BUILD_ROOT"/debs/bison_*.deb "$BUILD_ROOT"/debs/m4_*.deb; do
            [ -f "$f" ] && dpkg-deb --extract "$f" "$ext"
        done
        if [ -x "$ext/usr/bin/bison" ]; then
            cat > "$ext/usr/bin/yacc" <<YACC
#!/bin/sh
export BISON_PKGDATADIR="$ext/usr/share/bison"
exec "$ext/usr/bin/bison" -y "\$@"
YACC
            chmod +x "$ext/usr/bin/yacc"
        fi
    fi
    if [ -x "$ext/usr/bin/bison" ]; then
        # configure's AC_PROG_YACC finds this extracted `bison` directly on
        # PATH (it now ranks ahead of the plain-"yacc" case above) and wires
        # YACC="bison -y" literally into the Makefile -- calling the binary
        # straight, never through the wrapper script above that sets this
        # same variable. Without it exported here too, the extracted bison
        # looks for its skeleton/m4sugar files under its compiled-in default
        # (the SYSTEM /usr/share/bison, which does not exist on a bare
        # runner with no system bison installed) and fails every grammar
        # file with "cannot open ... m4sugar.m4" (reproduced).
        export BISON_PKGDATADIR="$ext/usr/share/bison"
    fi
    if [ -x "$ext/usr/bin/m4" ]; then
        export M4="$ext/usr/bin/m4"
    fi

    if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
        log "no C compiler found (gcc/clang) -- install one and re-run"
        return 1
    fi
    local cc="${CC:-}"
    [ -z "$cc" ] && cc="$(command -v gcc || command -v clang || command -v cc)"

    cd "$BUILD_ROOT/src"
    if [ ! -f "bash-$BASH32_VERSION.tar.gz" ]; then
        curl -sL -o "bash-$BASH32_VERSION.tar.gz" \
            "https://ftp.gnu.org/gnu/bash/bash-$BASH32_VERSION.tar.gz" || return 1
    fi
    rm -rf "bash-$BASH32_VERSION"
    tar -xzf "bash-$BASH32_VERSION.tar.gz"
    cd "bash-$BASH32_VERSION"

    # gcc 14 / recent clang both hard-error (not just warn) on K&R-style
    # function definitions and implicit declarations that were normal C in
    # 2007 when this bash release shipped -- -std=gnu89 plus a few -Wno-
    # flags turn those back into warnings, matching what building this
    # exact release always required on any C99+-strict modern compiler.
    CC="$cc" CFLAGS='-g -O2 -std=gnu89 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-return-type -Wno-error' \
        ./configure --without-bash-malloc --prefix="$INSTALL_ROOT" || return 1
    # CCFLAGS_FOR_BUILD (used only for the small `bashversion` support
    # program) does not inherit CFLAGS -- same -std=gnu89 fix needed there.
    make -j4 CFLAGS_FOR_BUILD='-std=gnu89 -Wno-implicit-function-declaration -Wno-implicit-int -Wno-return-type' || return 1
    cp bash "$BASH32_BIN"
}

if [ "${1:-}" = "--build-only" ]; then
    build_bash32
    exit $?
fi

if [ -n "${MC_BASH32:-}" ]; then
    BASH32_BIN="$MC_BASH32"
    if [ ! -x "$BASH32_BIN" ]; then
        log "MC_BASH32=$BASH32_BIN is not executable"
        exit 1
    fi
else
    build_bash32 || { log "== bash $BASH32_VERSION build FAILED =="; exit 1; }
fi

log "== bash 3.2 verification harness =="
log "MC_BASH=$BASH32_BIN"
"$BASH32_BIN" --version | head -1
log ""

if [ -z "${MEMCONTINUUM_PYTHON:-}" ]; then
    log "MEMCONTINUUM_PYTHON is not set -- see README.md Requirements"
    exit 1
fi

# The enrolled suites. The two hook suites were the original pair; the four
# installer/registry suites joined them once repo-init.sh, memcontinuum-
# setup.sh, memcontinuum-decide.sh, memcontinuum-state.sh and
# memcontinuum-update.sh grew enough shell to be worth the same bar the
# hooks are held to -- they are the scripts a macOS user runs by hand, and
# `bash -n` alone never catches a bash-4-ism on a path that is not taken.
#
# MC_BASH is what each suite invokes the entry-point script with. The nested
# calls (memcontinuum-update.sh -> repo-init.sh -> memcontinuum-decide.sh)
# follow on their own: those scripts shell out through "$BASH", the path of
# the interpreter already running them, rather than a bare `bash` off PATH
# that would hop back to the system's bash 5 halfway through.
BASH32_SUITES="tests.test_hooks tests.test_write_hooks tests.test_update tests.test_setup tests.test_repo_init tests.test_routing_rule_template"

cd "$REPO_ROOT"
MC_BASH="$BASH32_BIN" PYTHONPATH= "$MEMCONTINUUM_PYTHON" -m unittest $BASH32_SUITES
RC=$?

log ""
if [ $RC -eq 0 ]; then
    log "== CI SUMMARY: bash 3.2.57 ($BASH32_BIN) -- PASS =="
else
    log "== CI SUMMARY: bash 3.2.57 ($BASH32_BIN) -- FAIL (exit $RC) =="
fi
exit $RC
