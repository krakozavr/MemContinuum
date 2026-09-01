#!/usr/bin/env python
"""mc_settings_merge.py -- the ONE settings.json/settings.local.json hook-entry
merge implementation, shared by scripts/repo-init.sh (the seven per-project
hooks) and memcontinuum-setup.sh (the machine-level SessionStart detector).

Fix-round-4 F8: these used to be two divergent implementations. repo-init.sh
truncated the file in place with a bare `open(path, "w")` -- no atomicity, no
mode preservation; an interrupt mid-write left every session in the project
unable to load settings. memcontinuum-setup.sh's own merge_settings already
had the fix (tmp + os.replace, original-mode restore). One stdlib-only module
now; both callers invoke it with their own resolved python. Properties, for
both callers:
  * atomic write: a same-directory `<path>.tmp-memcontinuum` is written and
    fsync'd-by-close, then os.replace(tmp, path) -- never a truncate-in-place.
  * original file mode preserved on the rewritten file (a 0600 settings file
    must not come back 0644 under a default umask).
  * *.bak-memcontinuum backup (mode-preserving copy2) before every write that
    touches an existing file.
  * a malformed existing file (invalid JSON, non-object top level, a
    non-object "hooks", a non-list hooks.<event>) is REFUSED outright --
    the file is never touched. Raises MergeRefused; the CLI reports it to
    stderr and exits 2.
  * per-item removal, not per-group: a foreign hook sharing a matcher group
    with one of ours survives; a group left with nothing in it is dropped.
    Every other top-level key, and every event not named in EVENTS, is left
    completely untouched.
  * idempotent re-run: EVERY event named in EVENTS is swept (its survivors
    written back), whether or not ADD has anything new for it -- otherwise a
    re-run with less to add (e.g. repo-init.sh --code-root dropped to zero)
    would leave last run's entries stranded rather than replaced.

Two ways to use it:
  * As a library (`from mc_settings_merge import merge_settings,
    MergeRefused`) -- scripts/repo-init.sh's own python heredoc does this
    directly (it already builds the "groups to add" as native python dicts
    while rendering templates; round-tripping those through a CLI's JSON
    argv would only add a serialization step with nothing to show for it).
  * As a CLI, for a bash caller with no python objects to hand across --
    memcontinuum-setup.sh's merge_settings() shell function does this:

      mc_settings_merge.py PATH --events E1[,E2...] \\
          (--basenames s1[,s2...] [--project NAME] | --needle STR) \\
          --add JSON [--dry-run]

    Identity rule, exactly one of:
      --basenames LIST [--project NAME]
          An entry is ours iff its "command" contains one of LIST. With
          --project also given, an entry naming our scripts is further
          scoped: no MEMCONTINUUM_PROJECT= marker at all -> ours (legacy,
          pre-identity wiring -- safe to refresh); a marker -> must match
          --project (F1: a coexisting project's marked entries, e.g. its
          newfile-nudge.sh hook, must never be swept by another project's
          re-run of this same claude-dir).
      --needle STR
          Bare substring match, no project concept -- memcontinuum-setup.sh's
          one machine-wide detector entry.
    --add JSON is {"EventName": [group, ...]} -- new groups appended to each
    named event after the sweep (an event named in --events but absent from
    --add is still swept, just with nothing appended).
    Exit 0 on success ("ok" plus a short report on stdout); exit 2 with
    "REFUSED: ..." on stderr on a malformed existing file; exit 1 on any
    other failure (bad --add shape, bad arguments).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys


class MergeRefused(Exception):
    """The existing settings file is malformed in a way that must not be
    silently overwritten. str(e) is the REFUSED message, ready to print."""


def basenames_identity(basenames, project=None):
    """Returns an is_ours(cmd) callable matching scripts/repo-init.sh's
    identity rule (see module docstring's --basenames description).

    R7 fix, round 4 (Codex, probed): the project match used to be a bare
    `f"MEMCONTINUUM_PROJECT={project} " in cmd` substring check -- an
    entry's OWN --code-root path containing that literal text anywhere
    (e.g. a directory named .../MEMCONTINUUM_PROJECT=alpha/...) satisfied
    it even though the entry was never alpha's, so alpha's sweep deleted
    it. Anchored to an env-assignment position instead -- start-of-command
    or preceded by whitespace, followed by whitespace or end-of-string --
    matching either the bare form repo-init.sh actually renders
    (MEMCONTINUUM_PROJECT=<project>; PROJECT is charset-restricted to
    [A-Za-z0-9._-]+, so shlex.quote never adds quotes around it) or a
    hand-quoted MEMCONTINUUM_PROJECT='<project>' -- never a bare substring
    anywhere in the line, quoted or not.

    Regate round 2 (Codex): the "carries ANY marker at all?" pre-check
    must use the same anchor, or a legacy MARKERLESS entry whose
    --code-root path merely contains MEMCONTINUUM_PROJECT=... reads as
    marked-for-someone-else, survives the sweep, and gets duplicated when
    the refreshed groups are appended.
    """

    any_marker_re = re.compile(r"(?:^|\s)MEMCONTINUUM_PROJECT=")
    project_re = None
    if project is not None:
        esc = re.escape(project)
        project_re = re.compile(
            r"(?:^|\s)MEMCONTINUUM_PROJECT=(?:'" + esc + r"'|" + esc + r")(?=\s|$)"
        )

    def is_ours(cmd):
        if not any(name in cmd for name in basenames):
            return False
        if project is None:
            return True
        if any_marker_re.search(cmd) is None:
            return True
        return project_re.search(cmd) is not None

    return is_ours


def needle_identity(needle):
    return lambda cmd: needle in cmd


def merge_settings(path, events, is_ours, add=None, dry_run=False, log=None):
    """Merges hook entries into the settings file at PATH. See module
    docstring. Returns True on success (dry_run writes nothing). Raises
    MergeRefused on a malformed existing file (never touched) or ValueError
    on a malformed ADD. LOG, if given, is called once per report line."""
    add = add or {}
    if not isinstance(add, dict):
        raise ValueError("add must be a dict of event -> [group, ...]")

    def report(line):
        if log is not None:
            log(line)

    data = {}
    orig_mode = None
    if os.path.exists(path):
        orig_mode = os.stat(path).st_mode & 0o7777
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            raise MergeRefused(f"REFUSED: {path} is not valid JSON ({e}) -- fix or move it first")
        if not isinstance(data, dict):
            raise MergeRefused(f"REFUSED: {path} does not contain a JSON object at the top level")

    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    elif not isinstance(hooks, dict):
        raise MergeRefused(f"REFUSED: {path} has a non-object \"hooks\" value -- fix or move it first")
    else:
        hooks = dict(hooks)

    added_counts = {}
    for event in events:
        groups = hooks.get(event)
        if groups is None:
            groups = []
        elif not isinstance(groups, list):
            raise MergeRefused(f"REFUSED: {path} has a non-list hooks.{event} -- fix or move it first")

        kept = []
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            items = group.get("hooks")
            if not isinstance(items, list):
                kept.append(group)
                continue
            survivors = [
                it for it in items
                if not (isinstance(it, dict) and is_ours(str(it.get("command", ""))))
            ]
            if survivors:
                g = dict(group)
                g["hooks"] = survivors
                kept.append(g)
            # else: group became entirely ours -- drop it.

        new_groups = add.get(event, [])
        if not isinstance(new_groups, list):
            raise ValueError(f"add[{event!r}] must be a list of groups")
        added_counts[event] = len(new_groups)

        merged = kept + list(new_groups)
        if merged:
            hooks[event] = merged
        elif event in hooks:
            del hooks[event]

    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    if dry_run:
        report(f"  (dry-run) would merge into {path}:")
        for event in events:
            if added_counts[event]:
                report(f"    {event}: +{added_counts[event]} group(s)")
        return True

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if os.path.isfile(path):
        # copy2 preserves the original mode on the backup -- a 0600 settings
        # file's backup must not come back 0644 under a default umask.
        shutil.copy2(path, path + ".bak-memcontinuum")

    tmp = path + ".tmp-memcontinuum"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    if orig_mode is not None:
        os.chmod(tmp, orig_mode)
    os.replace(tmp, path)

    report(f"  wrote {path}")
    for event in events:
        if added_counts[event]:
            report(f"    merged {added_counts[event]} group(s) into hooks.{event}")
    return True


def main(argv):
    p = argparse.ArgumentParser(prog="mc_settings_merge.py")
    p.add_argument("path")
    p.add_argument("--events", required=True, help="comma-separated event names to sweep")
    p.add_argument("--basenames", default="", help="comma-separated script basenames (identity)")
    p.add_argument("--project", default=None, help="scope --basenames matches to this project")
    p.add_argument("--needle", default=None, help="bare substring identity (no --basenames)")
    p.add_argument("--add", default="{}", help='JSON {"Event": [group, ...]}')
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    events = [e for e in args.events.split(",") if e]
    basenames = [b for b in args.basenames.split(",") if b]
    if bool(basenames) == bool(args.needle):
        print("ERROR: pass exactly one of --basenames or --needle", file=sys.stderr)
        return 1

    try:
        add = json.loads(args.add)
    except json.JSONDecodeError as e:
        print(f"ERROR: --add is not valid JSON: {e}", file=sys.stderr)
        return 1

    is_ours = needle_identity(args.needle) if args.needle is not None else basenames_identity(basenames, args.project)

    try:
        merge_settings(args.path, events, is_ours, add=add, dry_run=args.dry_run, log=print)
    except MergeRefused as e:
        print(str(e), file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
