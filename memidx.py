#!/usr/bin/env python
"""memidx.py -- a project-agnostic decision-chain memory engine.

Indexes docs/SCHEMA.md markdown (topics with append-only `links:` chains, and
single-claim records like incidents/investigations) into a per-project
SQLite index (FTS5 + whole-record embeddings), and serves search/chain/
for-path/check against it.

Nothing here is tied to any particular project: the project name, the
markdown root, and the index location are all parameters (or the
MEMCONTINUUM_HOME environment variable), never hardcoded.

stdlib + pyyaml required always. fastembed (and numpy) are imported lazily,
only by the code paths that actually need vectors, so `for-path`, `chain`,
`check`, and `search --mode fts` stay fast and dependency-light.
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import yaml

import chunkers

AUTHORITIES = {
    "owner-verbatim",
    "owner-ratified",
    "agent-inference",
    "reviewer-finding",
    "code-derived",
}
STATUSES = {"active", "provisional", "superseded", "historical", "declined"}
KINDS = {"adopted", "declined", "reversed", "amended", "restored"}
# docs/SCHEMA.md.1 addendum SS1: per-link edges{} rel enum.
EDGE_RELS = {
    "supersedes",
    "preserves",
    "abandons",
    "challenged_by",
    "led_to",
    "applies_to",
    "reverses",
}
# docs/SCHEMA.md.1 addendum SS3: invariant kinds checked by `drift`.
INVARIANT_KINDS = {"pattern-absent", "no-bypass", "single-definition", "must-call"}

DEFAULT_PROJECT = "default"
# MEMCONTINUUM_HOME overrides the base directory that holds "<project>.sqlite"
# index files; defaults to ~/.memcontinuum. A per-call --db always wins over both
# (see resolve_db_path, which reads the environment variable fresh on every
# call so tests can override it per-case).
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_BODY_CHARS = 1500

# F1 (external-fix round, coordinator ruling 68): the decision index's own
# logical schema/content generation marker, bumped whenever a change needs
# every store to run one automatic full rebuild pass on its next reindex --
# covers this round's own stamp/generation scheme plus later tasks' evidence/
# link-row columns. A db stamped below this reads "upgrade-required" from
# decision_index_state (see below) until its next reindex.
# Bumped to 3 (F3, ruling 70): an unchanged-sha row would otherwise keep
# links.evidence NULL forever -- the migration guard only adds the COLUMN,
# it cannot retroactively parse markdown that never gets re-read.
# Bumped to 4 (F5, ruling 71/75): link rows (records.source_path/
# link_topic_path/link_id) are new this generation. A db stamped below
# generation 4 now reads "upgrade-required" (decision_index_state, below)
# until its next reindex -- readers warn instead of trusting it as fully
# current. cmd_reindex's own "migration probe" (run before open_db) now
# wires the reindex-side half: it forces one full content pass whenever
# EITHER the stored index_generation is older than this constant OR the
# records table itself still lacks the source_path column (the more
# specific, physically-verifiable signal -- catches a hand-restored or
# partially-migrated schema even when a generation number alone would say
# "current"). That full pass is what actually backfills links.evidence
# (generation 3's still-unwired promise) and builds every topic's link
# rows for the first time, without forcing a needless re-embed of an
# unchanged topic's own vector (see cmd_reindex's own comments).
CURRENT_INDEX_GENERATION = 4

# ---------------------------------------------------------------------------
# frontmatter parsing (shared by memidx and memlint)
# ---------------------------------------------------------------------------


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body text).

    Tolerant of files with no frontmatter (returns ({}, whole text)).
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith("---"):
        return {}, text
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_text = "".join(lines[1:end_idx])
    body = "".join(lines[end_idx + 1 :])
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        # Tolerant fallback for real-world notes with malformed frontmatter
        # (e.g. an unclosed quoted scalar): pull out simple top-level
        # `key: value` lines so at least title/name/type survive for search.
        # docs/SCHEMA.md canonical records are hand-authored and never hit this path.
        print(f"memidx: WARNING: {path}: malformed YAML frontmatter, using lenient fallback parse", file=sys.stderr)
        fm = {}
        for line in fm_text.splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
            if m and m.group(1) not in fm:
                fm[m.group(1)] = m.group(2).strip().strip("'\"")
    return fm, body.lstrip("\n")


def infer_type(root: Path, path: Path, fm: dict) -> str:
    t = fm.get("type")
    if t:
        return str(t)
    meta = fm.get("metadata") or {}
    t = meta.get("type")
    if t:
        return str(t)
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    if rel_parts:
        seg = rel_parts[0]
        return seg[:-1] if seg.endswith("s") else seg
    return "unknown"


def newest_active_link(links: list[dict]) -> dict | None:
    """First link in file order (newest-first convention) with status active."""
    for link in links:
        if link.get("status") == "active":
            return link
    return None


def derive_topic_status_authority(links: list[dict]) -> tuple[str | None, str | None]:
    current = newest_active_link(links)
    if current is None and links:
        current = links[0]
    if current is None:
        return None, None
    status = current.get("status")
    ruling = current.get("ruling") or {}
    authority = ruling.get("authority")
    if authority is None:
        rationale = current.get("rationale") or {}
        authority = rationale.get("authority")
    return status, authority


def build_record(root: Path, path: Path, fm: dict, body: str) -> dict:
    links = fm.get("links") or []
    is_topic = bool(links) or fm.get("type") == "topic"
    rtype = infer_type(root, path, fm)
    title = fm.get("title") or fm.get("name") or path.stem
    rid = fm.get("id") or path.stem
    area = fm.get("area") or ""
    code_refs = fm.get("code_refs") or []
    tags = fm.get("tags") or []

    if is_topic:
        status, authority = derive_topic_status_authority(links)
        topic = fm.get("topic") or rid
        # F5 (ruling 71): a topic's own aggregate ruling_text now carries
        # ONLY the current active link's text -- each link's own text lives
        # on its own link row (see _link_embed_items/insert_record_rows),
        # so concatenating every link's text here would duplicate content
        # already searchable per-link and blur which ruling is CURRENT.
        current_link = newest_active_link(links)
        ruling_text = ""
        if current_link:
            cruling = current_link.get("ruling") or {}
            crationale = current_link.get("rationale") or {}
            ruling_text = "\n".join(t for t in (cruling.get("text"), crationale.get("text")) if t)
    else:
        status = fm.get("status")
        authority = fm.get("authority")
        topic = fm.get("topic") or ""
        ruling_text = ""

    rec = {
        "path": str(path),
        "project": None,  # filled by caller
        "type": rtype,
        "id": str(rid),
        "title": str(title),
        "area": str(area),
        "topic": str(topic),
        "status": status,
        "authority": authority,
        "tags": json.dumps(tags),
        "code_refs": json.dumps(code_refs),
        "body": body,
        "ruling_text": ruling_text,
        "is_topic": is_topic,
        "current_field": fm.get("current"),
        "links": links,
        "concept": None,
    }

    # docs/SCHEMA.md.1 addendum SS4: concept records (type: concept, no links:).
    if rtype == "concept":
        rec["concept"] = {
            "owner_boundary": fm.get("owner_boundary") or "",
            "implemented_by": fm.get("implemented_by") or [],
            "tested_by": fm.get("tested_by") or [],
            "governed_by": fm.get("governed_by") or [],
            "involved_in": fm.get("involved_in") or [],
        }

    return rec


def embed_text_for(record: dict) -> str:
    body = record["body"][:EMBED_BODY_CHARS]
    return f"{record['title']}\n\n{body}"


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
-- Finding 2: records the single project this physical db file is allowed
-- to be opened for (see enforce_project_isolation) -- a plain key/value
-- table, not project-scoped itself (there is exactly one owning project
-- per db file, by construction).
CREATE TABLE IF NOT EXISTS db_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS records (
  path TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  mtime REAL NOT NULL,
  size INTEGER NOT NULL,
  project TEXT NOT NULL,
  type TEXT,
  id TEXT,
  title TEXT,
  area TEXT,
  topic TEXT,
  status TEXT,
  authority TEXT,
  tags TEXT,
  code_refs TEXT,
  body TEXT,
  ruling_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_project ON records(project);
CREATE INDEX IF NOT EXISTS idx_records_type ON records(project, type);

CREATE TABLE IF NOT EXISTS links (
  topic_path TEXT NOT NULL,
  topic_id TEXT,
  project TEXT NOT NULL,
  link TEXT NOT NULL,
  seq INTEGER NOT NULL,
  date TEXT,
  status TEXT,
  kind TEXT,
  reverses TEXT,
  reason_for_change TEXT,
  ruling_text TEXT,
  ruling_authority TEXT,
  ruling_source TEXT,
  rationale_text TEXT,
  rationale_authority TEXT,
  superseded_by TEXT,
  revisit_if TEXT,
  recorded_by TEXT,
  recorded_at TEXT,
  invariant TEXT,
  PRIMARY KEY (topic_path, link)
);
CREATE INDEX IF NOT EXISTS idx_links_topic ON links(topic_path);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  path UNINDEXED, project UNINDEXED, title, body, ruling_text
);

CREATE TABLE IF NOT EXISTS embeddings (
  path TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL
);

-- docs/SCHEMA.md.1 addendum SS1: typed cross-references, one row per {rel, to}
-- entry in a link's `edges:` list. topic_path is extra bookkeeping (not in
-- the addendum's minimal shape) so a reindex/delete can clear old rows.
CREATE TABLE IF NOT EXISTS edges (
  topic_path TEXT NOT NULL,
  project TEXT NOT NULL,
  from_ref TEXT NOT NULL,
  rel TEXT NOT NULL,
  to_ref TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_topic ON edges(topic_path);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(project, from_ref);

-- docs/SCHEMA.md.1 addendum SS2: one row per {id, text, status, since} entry in a
-- link's `assumptions:` list.
CREATE TABLE IF NOT EXISTS assumptions (
  topic_path TEXT NOT NULL,
  project TEXT NOT NULL,
  topic_id TEXT,
  link TEXT NOT NULL,
  aid TEXT NOT NULL,
  text TEXT,
  status TEXT,
  since TEXT
);
CREATE INDEX IF NOT EXISTS idx_assumptions_topic ON assumptions(topic_path);

-- docs/SCHEMA.md.1 addendum SS4: concept registry (type: concept records).
CREATE TABLE IF NOT EXISTS concepts (
  path TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  id TEXT,
  title TEXT,
  owner_boundary TEXT,
  implemented_by TEXT,
  tested_by TEXT,
  governed_by TEXT,
  involved_in TEXT
);
CREATE INDEX IF NOT EXISTS idx_concepts_project ON concepts(project, id);

-- one row per implemented_by/tested_by entry, for the same prefix/glob path
-- match `for-path` uses against code_refs. source_path is extra bookkeeping
-- (not in the addendum's minimal shape) so a reindex/delete can clear old
-- rows for one concept file without touching another's.
CREATE TABLE IF NOT EXISTS concept_paths (
  source_path TEXT NOT NULL,
  project TEXT NOT NULL,
  concept_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_concept_paths_source ON concept_paths(source_path);
CREATE INDEX IF NOT EXISTS idx_concept_paths_project ON concept_paths(project, concept_id);
"""


def ensure_links_invariant_column(conn: sqlite3.Connection) -> None:
    """Migration guard: a `links` table created by pre-v1.1 memidx.py has no
    `invariant` column. CREATE TABLE IF NOT EXISTS never adds columns to an
    existing table, so a real ~/.memcontinuum/<project>.sqlite built before this
    change would otherwise break on the next reindex."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(links)").fetchall()}
    if "invariant" not in cols:
        conn.execute("ALTER TABLE links ADD COLUMN invariant TEXT")


class DbProjectMismatchError(RuntimeError):
    """Finding 2: raised by open_db when the physical db file being opened
    already claims a DIFFERENT owning project than the one it's being
    opened for now."""


#: tables carrying a `project` column, inspected by _distinct_legacy_projects
#: below when a db has rows but no db_meta stamp yet (a real pre-Finding-2
#: db, or one whose db_meta row was lost) -- records is the primary/always-
#: populated one; the rest are unioned in for belt-and-suspenders coverage
#: since a partial/hand-edited legacy db could in principle have rows in one
#: without the other.
_PROJECT_SCOPED_TABLES = (
    "records", "links", "embeddings", "edges", "assumptions",
    "concepts", "concept_paths",
)


def _distinct_legacy_projects(conn: sqlite3.Connection) -> set[str]:
    """DISTINCT non-null `project` values found across every project-scoped
    table -- used only to figure out who ALREADY owns a db that has no
    db_meta stamp yet. A table missing entirely (schema drift on some very
    old db) is treated as contributing nothing, not as an error."""
    found: set[str] = set()
    for table in _PROJECT_SCOPED_TABLES:
        try:
            rows = conn.execute(f"SELECT DISTINCT project FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue
        found.update(row[0] for row in rows if row[0] is not None)
    return found


def enforce_project_isolation(conn: sqlite3.Connection, db_path: Path, project: str) -> None:
    """Finding 2 (MEDIUM, same-file --db project isolation): several
    tables here (records/embeddings/concepts/links) key rows by `path`
    alone, not (project, path) -- migrating every one of them to a
    composite key would touch nearly every table/query in this file for a
    collision only reachable via an explicit --db override in the first
    place (the default per-project "<project>.sqlite" filename already
    keeps two projects on separate files). The simpler, equally-safe fix:
    record the db's OWNING project the first time it's ever opened (in
    db_meta), and hard-refuse any later open under a DIFFERENT project --
    a same-file, different-project open is a named error, never a silent
    cross-project eviction/overwrite. Only enforced when `project` is
    given (open_db's default `project=None` skips this entirely, e.g. for
    tests/tools that just want to inspect a db file directly).

    HIGH (2026-08-31 review): a db can have rows but no db_meta stamp for a
    reason other than "brand new, zero data" -- it can be a real db built
    by a memidx.py version that predates db_meta ever being stamped (or one
    whose db_meta row was lost some other way). Blindly INSERTing the
    *requested* project as owner in that case would silently rewrite
    ownership to whatever the caller happened to pass, on exactly the kind
    of db this guard exists to protect. So an unstamped db is only ever
    stamped automatically when the existing data agrees it's safe: no rows
    at all (genuinely new), or rows for exactly one project and it's the
    one being requested. Any other case -- rows for one DIFFERENT project,
    or rows spanning several -- refuses instead of guessing."""
    row = conn.execute("SELECT value FROM db_meta WHERE key='project'").fetchone()
    if row is None:
        found = _distinct_legacy_projects(conn)
        if found and found != {project}:
            found_desc = ", ".join(repr(p) for p in sorted(found))
            raise DbProjectMismatchError(
                f"{db_path} has no db_meta project stamp yet, but already has rows for "
                f"{found_desc} -- refusing to guess ownership by stamping it for {project!r}. "
                f"Re-open it with --project matching the project found above (whichever one "
                f"actually owns this data), or start a fresh --db file for {project!r}."
            )
        conn.execute("INSERT INTO db_meta (key, value) VALUES ('project', ?)", (project,))
        conn.commit()
        return
    owner = row["value"]
    if owner != project:
        raise DbProjectMismatchError(
            f"{db_path} is owned by project {owner!r}, refusing to open it for project "
            f"{project!r} -- this db keys rows by path alone, so opening the SAME physical "
            f"file for a different --project would silently evict/overwrite that project's "
            f"rows. Pass a different --db, or use --project {owner!r}."
        )


def ensure_embeddings_embed_sha_column(conn: sqlite3.Connection) -> None:
    """Migration guard shaped like ensure_links_invariant_column: an
    embeddings row must remember the record sha it was computed FROM
    (Ruling 69's `embed_sha`), or the freshness join below has nothing to
    compare against and a stale vector can never be excluded from ranking."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()}
    if "embed_sha" not in cols:
        conn.execute("ALTER TABLE embeddings ADD COLUMN embed_sha TEXT")


def ensure_links_evidence_column(conn: sqlite3.Connection) -> None:
    """Migration guard shaped like ensure_links_invariant_column: a `links`
    row must carry its own `evidence` (F3, ruling 70), or invariant_enforcement_class
    has nothing to check a HOLD-eligible authority's validated evidence against."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(links)").fetchall()}
    if "evidence" not in cols:
        conn.execute("ALTER TABLE links ADD COLUMN evidence TEXT")


def ensure_records_link_columns(conn: sqlite3.Connection) -> bool:
    """Migration guard shaped like ensure_links_invariant_column. Returns
    True iff it just added the columns (a pre-F5 db reaching this schema),
    so cmd_reindex can force one full content pass -- without this, an
    upgraded topic whose sha never changes again would never grow its link
    rows. `source_path` is new and general-purpose (populated for EVERY
    row, topic or link -- see Task 5's Interfaces); it may already exist
    from a differently-shaped earlier migration in a dev checkout, hence
    the same guarded ADD COLUMN shape as the other two."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
    added = False
    if "source_path" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN source_path TEXT")
        added = True
    if "link_topic_path" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN link_topic_path TEXT")
        added = True
    if "link_id" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN link_id TEXT")
        added = True
    # Fix-round item 1 (coordinator review, IMPORTANT): this index cannot
    # live in SCHEMA_SQL -- open_db's executescript(SCHEMA_SQL) runs BEFORE
    # this guard adds link_topic_path, so a CREATE INDEX there would fail
    # outright on a legacy-shaped db (the column doesn't exist yet at that
    # point). CREATE INDEX IF NOT EXISTS here is always safe/idempotent,
    # run unconditionally (not gated on `added`) so a db that already had
    # the columns from an earlier partial migration but never got the
    # index still gets it now. _record_family/_collapse_link_duplicates's
    # batched WHERE path IN (...) lookups still scan by PRIMARY KEY (path);
    # this index is what makes _search_hits' hybrid-mode reverse lookups
    # (family membership by link_topic_path) and any future "every link
    # under this topic" query cheap.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_records_link_topic_path ON records(link_topic_path)")
    return added


def link_record_key(topic_path: str, link_id: str) -> str:
    """A collision-proof surrogate for a link row's records.path (its
    PRIMARY KEY) -- NOT f"{topic_path}#{link_id}" (rejected: '#' is legal
    in a real filename, so that scheme is not collision-proof). A hashed,
    prefixed token can never collide with anything walk_markdown yields
    (no real path is a 64-hex-char token prefixed "link:")."""
    digest = hashlib.sha256(f"{topic_path}\x00{link_id}".encode()).hexdigest()
    return f"link:{digest}"


def _link_embed_items(rec: dict) -> list[tuple[str, str, str]]:
    """(link_path, link_id, embed_text) for every link in a topic record
    that carries retrievable ruling/rationale text -- shared by
    cmd_reindex's embedding step and insert_record_rows's records/fts
    inserts, so the two never disagree about which links are retrievable."""
    if not rec["is_topic"]:
        return []
    out = []
    for link in rec["links"]:
        lid = str(link.get("link") or "")
        ruling = link.get("ruling") or {}
        rationale = link.get("rationale") or {}
        ltext = "\n".join(t for t in (ruling.get("text"), rationale.get("text")) if t)
        if lid and ltext:
            out.append((link_record_key(rec["path"], lid), lid, f"{rec['title']}\n\n{ltext}"))
    return out


def _record_family(conn, project: str, path: str) -> str:
    """A link row's own parent topic path, else the path itself -- the key
    that _collapse_link_duplicates/RRF group by so a topic and its own
    link rows are never scored as unrelated hits. A single-path lookup --
    used only where the caller already has a small, bounded set (cmd_
    search's own per-result-row loop, capped at args.limit); anything
    iterating a whole ranked candidate list uses _batch_record_meta below
    instead (fix-round item 2: one query per row here does not scale to
    up to 1000 candidates)."""
    row = conn.execute(
        "SELECT link_topic_path FROM records WHERE project=? AND path=?", (project, path)
    ).fetchone()
    return row["link_topic_path"] if row and row["link_topic_path"] else path


def _batch_record_meta(conn, project: str, paths: list[str]) -> dict[str, dict]:
    """path -> {"family": ..., "type": ..., "link_id": ...} for every path
    in `paths`, fetched in as few queries as the SQLite host-parameter
    limit allows -- ONE query per chunk of paths (chunk size comfortably
    under SQLite's default ~999 host-parameter limit), not one query per
    path. Fix-round item 2 (coordinator review, IMPORTANT):
    _collapse_link_duplicates/_record_family used to issue a SELECT per
    ranked candidate (up to 1000 in fts_ranked, the whole fresh corpus in
    vector_ranked), and _search_hits' hybrid branch added a second SELECT
    per item per channel (record_row_by_path) on top of that. `type`/
    `link_id` are fetched alongside `family` so _search_hits' hybrid
    fusion (which needs to know whether a path is a link row, and which
    link, for contributing_link_ids) is served by this SAME batched query
    instead of a second N-query pass."""
    out: dict[str, dict] = {}
    uniq = list(dict.fromkeys(paths))   # de-dup, preserve first-seen order
    CHUNK = 500
    for i in range(0, len(uniq), CHUNK):
        chunk = uniq[i:i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT path, link_topic_path, type, link_id FROM records "
            f"WHERE project=? AND path IN ({placeholders})",
            [project, *chunk],
        ).fetchall()
        for r in rows:
            out[r["path"]] = {
                "family": r["link_topic_path"] if r["link_topic_path"] else r["path"],
                "type": r["type"],
                "link_id": r["link_id"],
            }
    return out


def _collapse_link_duplicates(conn, project: str, ranked_paths: list[str]) -> list[str]:
    """Keep only the best-ranked (first) member of each topic-or-its-links
    family, preserving order. Applied to the fts/vector ranked list
    SEPARATELY, INSIDE fts_ranked/vector_ranked -- BEFORE their own cap and
    before RRF fusion (ruling 71): collapsing only a fused list lets the
    two per-mode lists pick different family winners and split credit
    right back apart. Batched (fix-round item 2): one query for the whole
    candidate list via _batch_record_meta, not one query per row."""
    meta = _batch_record_meta(conn, project, ranked_paths)
    seen: set[str] = set()
    out = []
    for p in ranked_paths:
        fam = meta[p]["family"] if p in meta else p
        if fam in seen:
            continue
        seen.add(fam)
        out.append(p)
    return out


def _run_decision_migration_guards(conn: sqlite3.Connection, db_path: Path, project: str | None) -> None:
    """Every additive-ALTER migration guard for the decision index, run
    unconditionally on every successful open (create via open_db, or
    noncreating via open_db_noncreating) -- idempotent against an
    already-migrated file (ruling 65). Replaces open_db's own inline calls
    to ensure_links_invariant_column/enforce_project_isolation, so there is
    exactly one guard list, not two; later tasks append one more guard call
    each here. `project=None` (open_db's own default, used by tests/tools
    that just want to inspect a db file directly) skips project-isolation
    enforcement, matching open_db's previous behavior exactly."""
    ensure_links_invariant_column(conn)
    ensure_embeddings_embed_sha_column(conn)
    ensure_links_evidence_column(conn)
    ensure_records_link_columns(conn)
    if project is not None:
        enforce_project_isolation(conn, db_path, project)


def open_db(db_path: Path, project: str | None = None) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    _run_decision_migration_guards(conn, db_path, project)
    return conn


def open_db_noncreating(db_path: Path, project: str) -> sqlite3.Connection | None:
    """SQLite URI mode=rw: opens an EXISTING file for read/write, NEVER
    creates one -- the atomic fix for the TOCTOU window an exists() check
    followed by plain connect()/open_db() still has (Codex, ruling 68).
    Returns None when the file doesn't exist (or vanished in the race)."""
    try:
        conn = sqlite3.connect(f"file:{quote(db_path.as_posix())}?mode=rw", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    _run_decision_migration_guards(conn, db_path, project)
    return conn


def resolve_db_path(args) -> Path:
    if getattr(args, "db", None):
        return Path(args.db).expanduser().resolve()
    base = Path(os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum")))
    return base / f"{args.project}.sqlite"


def decision_index_state(db_path: Path, project: str, root: Path | None = None) -> str:
    """"missing" | "uninitialized" | "upgrade-required" | "stale" | "current"
    -- ruling 68's five-state model. `root`, when given, additionally
    checks for on-disk drift via the existing _index_has_drift (the fifth
    state, "stale") -- omitted by root-less readers for cheapness (and
    because a root-less reader has nothing to walk in the first place)."""
    conn = open_db_noncreating(db_path, project)
    if conn is None:
        return "missing"
    try:
        stamp = conn.execute("SELECT value FROM db_meta WHERE key='last_reindexed_at'").fetchone()
        has_rows = conn.execute("SELECT 1 FROM records WHERE project=? LIMIT 1", (project,)).fetchone()
        if stamp is None:
            return "uninitialized" if has_rows is None else "upgrade-required"
        gen_row = conn.execute("SELECT value FROM db_meta WHERE key='index_generation'").fetchone()
        generation = int(gen_row["value"]) if gen_row else 1
        if generation < CURRENT_INDEX_GENERATION:
            return "upgrade-required"
        if root is not None and _index_has_drift(conn, root, project):
            return "stale"
        return "current"
    finally:
        conn.close()


def _decision_reply(cmd_name: str, args, state: str) -> int:
    """The shared refuse-and-print helper for `missing`/`uninitialized`: no
    query is even attempted. The stderr diagnostic always prints (a caller
    piping stdout for `--json` still needs to see WHY it got an empty
    envelope); `--json` additionally emits the refusal envelope on stdout.
    Returns 1 (the reader's own refusal exit code -- see `for-path`'s own
    distinct 3/4 codes, which don't go through this)."""
    print(
        f"{cmd_name}: the decision index is {state} for project "
        f"{args.project!r} -- run `reindex --root <path>` first",
        file=sys.stderr,
    )
    if getattr(args, "json", False):
        print(json.dumps({"state": state, "results": []}, indent=2))
    return 1


def _decision_warn(cmd_name: str, args, state: str) -> None:
    """A positive match off an `upgrade-required`/`stale` index is still
    real, trustworthy evidence (ruling 68) -- the caller keeps querying and
    injecting, this just surfaces the caveat on stderr."""
    print(
        f"{cmd_name}: index {state} (results may be incomplete) -- run reindex",
        file=sys.stderr,
    )


def delete_record_rows(conn: sqlite3.Connection, path: str, *, keep_embedding: bool = False) -> None:
    """F5: a topic's own link rows (records.link_topic_path == path) cascade
    with it -- select their paths first, then delete fts/embeddings/records
    for each, all inside this same open connection/transaction (Codex-
    pending item 4). `keep_embedding` applies uniformly to the topic's own
    row AND every one of its link rows: a --no-embed/--auto edit keeps
    every vector in the family physically, stale-excluded from ranking by
    the freshness join, not deleted-then-hoped-to-refill."""
    link_paths = [
        r["path"] for r in
        conn.execute("SELECT path FROM records WHERE link_topic_path=?", (path,)).fetchall()
    ]
    for lp in link_paths:
        conn.execute("DELETE FROM fts WHERE path=?", (lp,))
        if not keep_embedding:
            conn.execute("DELETE FROM embeddings WHERE path=?", (lp,))
        conn.execute("DELETE FROM records WHERE path=?", (lp,))
    conn.execute("DELETE FROM fts WHERE path=?", (path,))
    if not keep_embedding:
        conn.execute("DELETE FROM embeddings WHERE path=?", (path,))
    conn.execute("DELETE FROM records WHERE path=?", (path,))
    conn.execute("DELETE FROM links WHERE topic_path=?", (path,))
    conn.execute("DELETE FROM edges WHERE topic_path=?", (path,))
    conn.execute("DELETE FROM assumptions WHERE topic_path=?", (path,))
    conn.execute("DELETE FROM concepts WHERE path=?", (path,))
    conn.execute("DELETE FROM concept_paths WHERE source_path=?", (path,))


def insert_record_rows(conn: sqlite3.Connection, project: str, rec: dict, sha: str, mtime: float, size: int) -> None:
    conn.execute(
        """INSERT INTO records
           (path, sha256, mtime, size, project, type, id, title, area, topic,
            status, authority, tags, code_refs, body, ruling_text,
            source_path, link_topic_path, link_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rec["path"], sha, mtime, size, project, rec["type"], rec["id"], rec["title"],
            rec["area"], rec["topic"], rec["status"], rec["authority"], rec["tags"],
            rec["code_refs"], rec["body"], rec["ruling_text"],
            rec["path"], None, None,
        ),
    )
    conn.execute(
        "INSERT INTO fts (path, project, title, body, ruling_text) VALUES (?,?,?,?,?)",
        (rec["path"], project, rec["title"], rec["body"], rec["ruling_text"]),
    )
    if rec["is_topic"]:
        for seq, link in enumerate(rec["links"]):
            ruling = link.get("ruling") or {}
            rationale = link.get("rationale") or {}
            invariant = link.get("invariant")
            conn.execute(
                """INSERT OR REPLACE INTO links
                   (topic_path, topic_id, project, link, seq, date, status, kind, reverses,
                    reason_for_change, ruling_text, ruling_authority, ruling_source,
                    rationale_text, rationale_authority, superseded_by, revisit_if,
                    recorded_by, recorded_at, invariant, evidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec["path"], rec["id"], project, link.get("link"), seq,
                    str(link.get("date") or ""), link.get("status"), link.get("kind"),
                    link.get("reverses"), link.get("reason_for_change"),
                    ruling.get("text"), ruling.get("authority"), ruling.get("source"),
                    rationale.get("text"), rationale.get("authority"),
                    link.get("superseded_by"), json.dumps(link.get("revisit_if") or []),
                    link.get("recorded_by"), str(link.get("recorded_at") or ""),
                    json.dumps(invariant) if invariant else None,
                    json.dumps(link.get("evidence") or []) if link.get("evidence") else None,
                ),
            )

            from_ref = f"{rec['id']}/{link.get('link')}"
            for edge in link.get("edges") or []:
                conn.execute(
                    "INSERT INTO edges (topic_path, project, from_ref, rel, to_ref) VALUES (?,?,?,?,?)",
                    (rec["path"], project, from_ref, edge.get("rel"), edge.get("to")),
                )
            for a in link.get("assumptions") or []:
                conn.execute(
                    """INSERT INTO assumptions
                       (topic_path, project, topic_id, link, aid, text, status, since)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        rec["path"], project, rec["id"], link.get("link"),
                        str(a.get("id") or ""), a.get("text"), a.get("status"),
                        str(a.get("since") or "") or None,
                    ),
                )

        # F5 (ruling 71): one searchable row per link -- source_path names
        # the parent topic file (so reindex's own "existing"/removal
        # detection and _index_has_drift/cmd_check never mistake it for a
        # missing source file), link_topic_path is the same value kept as
        # its own column for display/lookup, link_id is that link's own id.
        for link_path, link_id, embed_text in _link_embed_items(rec):
            link = next(l for l in rec["links"] if str(l.get("link")) == link_id)
            ruling = link.get("ruling") or {}
            rationale = link.get("rationale") or {}
            link_ruling_text = embed_text.split("\n\n", 1)[1]
            conn.execute(
                """INSERT OR REPLACE INTO records
                   (path, sha256, mtime, size, project, type, id, title, area, topic,
                    status, authority, tags, code_refs, body, ruling_text,
                    source_path, link_topic_path, link_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (link_path, sha, mtime, size, project, "link", rec["id"], rec["title"], rec["area"],
                 rec["topic"], link.get("status"), ruling.get("authority") or rationale.get("authority"),
                 rec["tags"], "[]", "", link_ruling_text, rec["path"], rec["path"], link_id),
            )
            conn.execute("INSERT INTO fts (path, project, title, body, ruling_text) VALUES (?,?,?,?,?)",
                         (link_path, project, rec["title"], "", link_ruling_text))

    if rec.get("concept"):
        c = rec["concept"]
        conn.execute(
            """INSERT INTO concepts
               (path, project, id, title, owner_boundary, implemented_by, tested_by,
                governed_by, involved_in)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                rec["path"], project, rec["id"], rec["title"], c["owner_boundary"],
                json.dumps(c["implemented_by"]), json.dumps(c["tested_by"]),
                json.dumps(c["governed_by"]), json.dumps(c["involved_in"]),
            ),
        )
        for ref in c["implemented_by"]:
            conn.execute(
                "INSERT INTO concept_paths (source_path, project, concept_id, kind, path) VALUES (?,?,?,?,?)",
                (rec["path"], project, rec["id"], "implemented_by", ref),
            )
        for ref in c["tested_by"]:
            conn.execute(
                "INSERT INTO concept_paths (source_path, project, concept_id, kind, path) VALUES (?,?,?,?,?)",
                (rec["path"], project, rec["id"], "tested_by", ref),
            )


def pack_vector(vec) -> bytes:
    import array

    return array.array("f", [float(x) for x in vec]).tobytes()


def unpack_vector(blob: bytes):
    import array

    a = array.array("f")
    a.frombytes(blob)
    return list(a)


# ---------------------------------------------------------------------------
# reindex
# ---------------------------------------------------------------------------


# Store-side walk pruning, the counterpart to CODE_SKIP_DIR_NAMES below.
# Shared by reindex, check, unmapped's drift self-heal -- and by memlint.py,
# which imports this function: a session buffer must not be linted as a topic
# either.
# Records never live in a dot-directory, but plenty of noise does: `.git`
# itself, a project's `.claude/` settings, and -- the case that forced this --
# a `.remember/now.md` session buffer sitting in a store root, which reindex
# was indexing as a record and then serving in `search` results alongside real
# rulings. Pruning by directory name keeps the walk cheap and needs no
# per-file check. `os.walk` never prunes the root it is given, so a store that
# legitimately lives at e.g. `~/.memory/` still indexes in full.
#
# Known and accepted gap (reviewer finding, 2026-08-31): with followlinks=True
# a NON-hidden directory that symlinks AT a hidden one is still walked, since
# the prune tests the local name. Closing it would cost a realpath() per
# directory on every walk to defend against a store deliberately aliasing its
# own noise, which nothing observed does. A hidden directory reached by its own
# name is pruned whether or not it is a symlink.
STORE_SKIP_DIR_NAMES = {"node_modules"}


def walk_markdown(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in STORE_SKIP_DIR_NAMES
        ]
        for fname in filenames:
            # Hidden files get the same treatment as hidden directories, and
            # for the same reason: a dotfile is tooling's, not a record.
            if fname.endswith(".md") and not fname.startswith("."):
                yield Path(dirpath) / fname


def cmd_reindex(args) -> int:
    """F5: full replacement of Task 2's version (see that function's own
    former docstring, now gone) -- link rows, source_path/link_topic_path/
    link_id, the generation-driven migration probe, and Ruling 73's
    --auto-recomputes-mode-when-it-changed-something fix all land here."""
    root = Path(args.root).resolve()
    if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
        print(f"reindex: {root} is not a readable directory -- nothing was changed", file=sys.stderr)
        return 2
    db_path = resolve_db_path(args)

    # F5/Ruling 75 migration probe -- runs on a SEPARATE plain connection,
    # BEFORE open_db (which would immediately ALTER the missing columns in
    # via ensure_records_link_columns, destroying the very signal this
    # probe needs to read). Two independent triggers, either one forces a
    # full content pass this run: (1) the records table itself still lacks
    # source_path -- the more specific, physically-verifiable signal, also
    # correct for a hand-restored/partially-migrated schema even when the
    # generation number alone would say "current"; (2) the stored
    # index_generation is older than CURRENT_INDEX_GENERATION -- catches a
    # db that already has the new columns (e.g. one generation-3 store
    # whose evidence backfill, promised by that generation bump, was never
    # actually wired until this task) but was never told to re-parse its
    # unchanged-sha topics. A db with no db_meta at all (brand new) hits
    # neither branch here -- it has no rows yet for "existing" to matter.
    migrated = False
    if db_path.exists():
        probe = sqlite3.connect(str(db_path))
        try:
            cols = {row[1] for row in probe.execute("PRAGMA table_info(records)").fetchall()}
            if cols and "source_path" not in cols:
                migrated = True
            else:
                gen_row = probe.execute(
                    "SELECT value FROM db_meta WHERE key='index_generation'"
                ).fetchone()
                if gen_row is not None:
                    # Fix-round item 6 (coordinator review): a corrupted
                    # stamp must not crash the whole reindex -- int() on
                    # garbage raises ValueError, which is NOT a
                    # sqlite3.OperationalError and would otherwise escape
                    # the except below uncaught. Treat "not a valid
                    # integer" the same as "older than current": force the
                    # full content pass (the safe direction -- a corrupted
                    # stamp is evidence this db's own bookkeeping cannot be
                    # trusted, so re-parsing everything is strictly safer
                    # than trusting a row count / sha match that might
                    # itself be from a half-written state).
                    try:
                        generation = int(gen_row[0])
                    except (TypeError, ValueError):
                        print(
                            f"reindex: index_generation stamp {gen_row[0]!r} is not a valid "
                            f"integer -- treating the index as older than current and forcing "
                            f"a full content pass",
                            file=sys.stderr,
                        )
                        migrated = True
                    else:
                        if generation < CURRENT_INDEX_GENERATION:
                            migrated = True
        except sqlite3.OperationalError:
            pass
        finally:
            probe.close()

    conn = open_db(db_path, project=args.project)   # runs _run_decision_migration_guards,
                                                       # incl. ensure_records_link_columns
    t0 = time.time()
    no_embed = args.no_embed or getattr(args, "auto", False)
    # content_full stands in for Task 2's plain `args.full` everywhere a
    # record's CONTENT (records/fts/links/link-rows) needs a rewrite --
    # `migrated` forces this once, so an upgrading topic grows its link
    # rows (and gets links.evidence backfilled) even when its sha never
    # changes again. It does NOT stand in for `args.full` in the
    # embedding-queue guard below -- a migration-only pass must never
    # re-embed an already-current vector, only an explicit --full may
    # force that (Finding 7's fix, carried from Task 2's own history).
    content_full = args.full or migrated

    # NULL-aware real-file predicate: `source_path IS NULL` covers a row
    # that ensure_records_link_columns just ALTERed into existence this
    # very open() (a genuine pre-F5 legacy row -- its NEW columns are NULL
    # until this pass's own rewrite populates them), `source_path = path`
    # covers every already-migrated topic row. A link row's source_path
    # always names its PARENT (never itself, and never NULL once written),
    # so it is excluded either way -- this is what keeps a link row from
    # ever being mistaken for a missing/changed source file (ruling 66).
    existing = {
        row["path"]: (row["sha256"], row["mtime"])
        for row in conn.execute(
            "SELECT path, sha256, mtime FROM records WHERE project=? "
            "AND (source_path IS NULL OR source_path = path)",
            (args.project,),
        )
    }
    embedded_shas = {
        row["path"]: row["embed_sha"]
        for row in conn.execute("SELECT path, embed_sha FROM embeddings WHERE project=?", (args.project,))
    }

    files = sorted(walk_markdown(root))
    seen = set()
    to_embed_paths: list[str] = []
    to_embed_texts: list[str] = []
    pending: list[tuple[dict, str, float, int]] = []
    backfill_only: list[tuple[str, dict]] = []
    unchanged = 0

    for f in files:
        path_str = str(f)
        seen.add(path_str)
        stat = f.stat()
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        prev = existing.get(path_str)
        sha_unchanged = prev is not None and not content_full and prev[0] == sha
        needs_backfill = (not no_embed) and sha_unchanged and embedded_shas.get(path_str) != sha
        if sha_unchanged and not needs_backfill:
            unchanged += 1
            continue
        fm, body = parse_frontmatter(f)
        rec = build_record(root, f, fm, body)
        rec["project"] = args.project
        if sha_unchanged and needs_backfill:
            backfill_only.append((path_str, rec))
            to_embed_paths.append(path_str); to_embed_texts.append(embed_text_for(rec))
            for link_path, _lid, ltext in _link_embed_items(rec):
                if embedded_shas.get(link_path) != sha:
                    to_embed_paths.append(link_path); to_embed_texts.append(ltext)
            continue
        pending.append((rec, sha, stat.st_mtime, stat.st_size))
        if not no_embed:
            # Finding 7's fix: a record reaches `pending` whenever its sha
            # genuinely changed OR content_full forced a content rewrite
            # (migration/--full) with the sha unchanged. Re-embedding the
            # topic's OWN text is only warranted when args.full was asked
            # for explicitly, or the embedding is actually missing/stale
            # for the CURRENT sha -- for a genuine edit those are always
            # true (a fresh sha never has a matching prior embedding), so
            # this only skips the migration-only, sha-unchanged case.
            if args.full or embedded_shas.get(path_str) != sha:
                to_embed_paths.append(path_str); to_embed_texts.append(embed_text_for(rec))
            for link_path, _lid, ltext in _link_embed_items(rec):
                # a brand-new link row (the migration case) always
                # qualifies -- embedded_shas has no entry for it yet.
                if args.full or embedded_shas.get(link_path) != sha:
                    to_embed_paths.append(link_path); to_embed_texts.append(ltext)

    vectors_by_path: dict[str, bytes] = {}
    if to_embed_texts:
        vecs = compute_embeddings(to_embed_texts)
        for p, v in zip(to_embed_paths, vecs):
            vectors_by_path[p] = pack_vector(v)

    def _upsert_embedding(path: str, sha_for: str) -> bool:
        if path not in vectors_by_path:
            return False
        vec = vectors_by_path[path]
        conn.execute(
            "INSERT OR REPLACE INTO embeddings (path, project, dim, embed_sha, vector) VALUES (?,?,?,?,?)",
            (path, args.project, len(unpack_vector(vec)), sha_for, vec),
        )
        return True

    backfilled = 0
    for path_str, rec in backfill_only:
        sha = existing[path_str][0]
        if _upsert_embedding(path_str, sha):
            backfilled += 1
        for link_path, _lid, _t in _link_embed_items(rec):
            if _upsert_embedding(link_path, sha):
                backfilled += 1

    added = 0
    changed = 0
    for rec, sha, mtime, size in pending:
        is_new = rec["path"] not in existing
        # keep_embedding used to be `no_embed and not is_new` (Task 2): "if
        # we're not embedding at all this pass, keep the stale vector".
        # That is no longer sufficient on its own -- the guard above means
        # `not no_embed` no longer guarantees THIS path is being
        # re-embedded (a migration-only pass may leave it untouched), so
        # the real question is "is a fresh vector for this exact path
        # about to be written", which `rec["path"] in vectors_by_path`
        # answers directly.
        will_refresh_embedding = rec["path"] in vectors_by_path
        keep_embedding = not is_new and not will_refresh_embedding
        delete_record_rows(conn, rec["path"], keep_embedding=keep_embedding)
        insert_record_rows(conn, args.project, rec, sha, mtime, size)
        _upsert_embedding(rec["path"], sha)
        for link_path, _lid, _t in _link_embed_items(rec):
            _upsert_embedding(link_path, sha)
        if is_new:
            added += 1
        else:
            changed += 1

    removed_paths = set(existing.keys()) - seen
    for p in removed_paths:
        delete_record_rows(conn, p)   # keep_embedding=False always -- a removed record's
                                       # vector is dropped, never kept stale.

    # Ruling 69: embedding_mode is recomputed from ACTUAL fresh coverage on
    # every run except a NO-OP --auto pass -- one uniform rule replaces
    # Revision 3's special-cased "only downgrade under --no-embed when
    # mutated" branch, and naturally handles "a backfill restores full"
    # for free (no separate case needed: fresh coverage is just recomputed
    # and happens to be 100%).
    # Ruling 73: an --auto run that actually changed a row (added/changed/
    # removed -- backfills never happen under --auto, since no_embed is
    # forced True whenever auto is True) must still recompute mode from
    # real coverage -- `full` must never keep standing over a vector that
    # this very --auto pass just made stale. Only a genuine no-op --auto
    # pass (nothing changed) still skips this block entirely, matching the
    # original "an internal heal never announces or forces a mode change
    # for a change that didn't happen" intent.
    auto = getattr(args, "auto", False)
    mode_relevant_change = bool(added or changed or removed_paths)
    if not auto or mode_relevant_change:
        mode_row = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
        mode_now = mode_row["value"] if mode_row else "none"
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE project=?", (args.project,)
        ).fetchone()["n"]
        fresh = conn.execute(
            "SELECT COUNT(*) AS n FROM records r JOIN embeddings e "
            "ON e.path=r.path AND e.embed_sha=r.sha256 WHERE r.project=?",
            (args.project,),
        ).fetchone()["n"]
        if total == 0 or fresh == 0:
            mode = "none"
        elif fresh == total:
            mode = "full"
        else:
            mode = "partial"
        if mode != mode_now:
            conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('embedding_mode', ?)", (mode,))
            if mode_now == "full" and mode != "full":
                print(f"reindex: embeddings are now incomplete for {args.project}; embedding mode set "
                      f"to {mode} (run without --no-embed to restore)")

    # F1 (Codex-pending item 1): the successful-reindex stamp and the
    # logical index generation land in the SAME transaction as every other
    # write above -- a 0-change run still stamps, so a "reindexed nothing
    # changed" pass still moves the db out of "upgrade-required"/
    # "uninitialized" into "current".
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('last_reindexed_at', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', ?)",
        (str(CURRENT_INDEX_GENERATION),),
    )
    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    print(f"reindex: {len(files)} files scanned, {added} added, {changed} changed, "
          f"{unchanged} unchanged, {len(removed_paths)} removed, {backfilled} embedding(s) backfilled, "
          f"{elapsed:.3f}s")
    return 0


def compute_embeddings(texts: list[str]):
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    return list(model.embed(texts))


def compute_query_embedding(text: str):
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    return list(model.query_embed([text]))[0]


def cosine(a: list[float], b: list[float]) -> float:
    """Returns a plain Python float, always -- fastembed's query_embed
    yields a numpy array (numpy.float32 elements), so an un-cast result
    here silently produces a numpy.float32 that `json.dumps` cannot
    serialize (TypeError: Object of type float32 is not JSON serializable)
    the moment a caller's score reaches --json output. Pre-existing on the
    markdown `search --mode vector --json` path too (same helper); fixed
    here since code-search inherits the identical bug via the same
    cosine()/compute_query_embedding() pair."""
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return float(dot / (na * nb))


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def build_filter_clause(args, include_project: bool = True) -> tuple[str, list]:
    """`include_project=False` omits the `project=?` clause entirely --
    fts_ranked/vector_ranked (F5) already have their own project predicate
    in a differently-aliased WHERE (records.project=? / e.project=?), so
    they call this with include_project=False to avoid a second, redundant
    one and add whatever this returns as their own extra AND-clause.
    filtered_paths (unchanged caller) keeps the default True."""
    clauses = []
    params: list = []
    if include_project:
        clauses.append("project=?")
        params.append(args.project)
    if args.status:
        clauses.append("status IN (%s)" % ",".join("?" * len(args.status)))
        params.extend(args.status)
    if args.type:
        clauses.append("type IN (%s)" % ",".join("?" * len(args.type)))
        params.extend(args.type)
    if args.area:
        clauses.append("area=?")
        params.append(args.area)
    if args.topic:
        clauses.append("topic=?")
        params.append(args.topic)
    if args.authority:
        clauses.append("authority=?")
        params.append(args.authority)
    return " AND ".join(clauses), params


def filtered_paths(conn, args) -> set[str]:
    where, params = build_filter_clause(args)
    rows = conn.execute(f"SELECT path FROM records WHERE {where}", params).fetchall()
    return {r["path"] for r in rows}


def record_row_by_path(conn, path: str):
    return conn.execute("SELECT * FROM records WHERE path=?", (path,)).fetchone()


def snippet_for(row) -> str:
    """F5: a link row's own body is always "" (its content lives in
    ruling_text) -- fall back to ruling_text so a link-row hit never
    reports an empty snippet."""
    text = row["body"] or row["ruling_text"] or ""
    return text.strip().replace("\n", " ")[:200]


def fts_ranked(conn, query: str, project: str, filter_clause: str = "", filter_params: list | None = None) -> list[str]:
    """F5/F7 (ruling 71): status/type/area/topic/authority filtering (from
    build_filter_clause(args, include_project=False), passed in by the
    caller) is applied INSIDE this query's own WHERE, and parent-topic
    collapse runs on the full filtered/ranked result, BOTH before the real
    per-channel cap below -- not, as before F5, a fixed LIMIT 200 applied
    BEFORE any status/type filtering, which a flood of non-matching-status
    link rows (this task's own candidate-count multiplier) could push a
    real match behind."""
    q = fts_escape(query)
    where = "fts MATCH ? AND records.project=?"
    params: list = [q, project]
    if filter_clause:
        where += f" AND {filter_clause}"
        params.extend(filter_params or [])
    rows = conn.execute(
        f"SELECT fts.path AS path FROM fts JOIN records ON records.path=fts.path "
        f"WHERE {where} ORDER BY bm25(fts) LIMIT 1000",
        # 1000, not 200 -- a generous raw ceiling, not a candidate-starving
        # one. Filtering already happened in this same query's WHERE, so
        # this only guards a pathological match count; it never discards a
        # real match before it's even considered (F7's actual bug).
        params,
    ).fetchall()
    ranked = [r["path"] for r in rows]
    collapsed = _collapse_link_duplicates(conn, project, ranked)   # parent-topic collapse BEFORE the real cap
    return collapsed[:200]


def fts_escape(query: str) -> str:
    terms = [t for t in query.replace('"', " ").split() if t]
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


def vector_ranked(conn, query: str, project: str, filter_clause: str = "", filter_params: list | None = None) -> list[tuple[str, float]]:
    """Ruling 69: joins embeddings to records on embed_sha == sha256 -- a
    stale vector (kept physically across a --no-embed/--auto edit) never
    reaches the scoring loop at all, not merely scores lower. F5/F7 (ruling
    71): the SAME filter_clause fts_ranked takes, joined into this query's
    own WHERE alongside the freshness join, plus the same collapse-before-
    return (vector mode never had a starvation-causing cap -- it scores
    every fresh row -- but collapse must still happen here, not as a
    separate post-filter step, or the two channels could disagree about
    which family member survives)."""
    qvec = compute_query_embedding(query)
    where = "e.project=?"
    params: list = [project]
    if filter_clause:
        where += f" AND {filter_clause}"
        params.extend(filter_params or [])
    rows = conn.execute(
        f"SELECT e.path AS path, e.vector AS vector FROM embeddings e "
        f"JOIN records r ON r.path = e.path AND r.sha256 = e.embed_sha "
        f"WHERE {where}",
        params,
    ).fetchall()
    scored = [(r["path"], cosine(qvec, unpack_vector(r["vector"]))) for r in rows]
    scored.sort(key=lambda t: t[1], reverse=True)
    collapsed = _collapse_link_duplicates(conn, project, [p for p, _ in scored])
    score_by_path = dict(scored)
    return [(p, score_by_path[p]) for p in collapsed]


def _search_hits(conn, args, extra_where: str, extra_params: list) -> tuple[list[tuple[str, float]], dict[str, dict[str, str]]]:
    """The mode-dispatch + RRF fusion core shared by cmd_search and (via
    the test module's own _run_search, which delegates here rather than
    reimplementing ranking) tests/test_memidx.py -- one ranking
    implementation, not two that can silently drift apart. Returns
    (results, contributing): `contributing` maps a family key to
    {"fts": link_id, "vector": link_id} whenever hybrid mode's two
    channels picked DIFFERENT link members of the same topic family."""
    results: list[tuple[str, float]] = []
    contributing: dict[str, dict[str, str]] = {}
    if args.mode == "fts":
        ranked = fts_ranked(conn, args.query, args.project, extra_where, extra_params)
        results = [(p, float(len(ranked) - i)) for i, p in enumerate(ranked)]
    elif args.mode == "vector":
        results = vector_ranked(conn, args.query, args.project, extra_where, extra_params)
    elif args.mode == "hybrid":
        fts_list = fts_ranked(conn, args.query, args.project, extra_where, extra_params)
        vec_list = [p for p, _ in vector_ranked(conn, args.query, args.project, extra_where, extra_params)]
        # Fix-round item 2 (coordinator review): ONE batched query for
        # both channels' combined candidate set, replacing what used to be
        # a _record_family call PLUS a record_row_by_path call per item
        # per channel (two N-query passes on top of fts_ranked/
        # vector_ranked's own now-batched internal collapse).
        meta = _batch_record_meta(conn, args.project, fts_list + vec_list)
        k = 60
        scores: dict[str, float] = {}
        family_winner: dict[str, str] = {}
        for channel_name, lst in (("fts", fts_list), ("vector", vec_list)):
            for i, p in enumerate(lst):
                m = meta.get(p) or {"family": p, "type": None, "link_id": None}
                fam = m["family"]
                scores[fam] = scores.get(fam, 0.0) + 1.0 / (k + i + 1)
                family_winner.setdefault(fam, p)
                if m["type"] == "link":
                    contributing.setdefault(fam, {})[channel_name] = m["link_id"]
        results = sorted(((family_winner[fam], s) for fam, s in scores.items()), key=lambda t: t[1], reverse=True)
    else:
        raise ValueError(f"unknown mode {args.mode}")
    return results, contributing


def cmd_search(args) -> int:
    db_path = resolve_db_path(args)
    state = decision_index_state(db_path, args.project)   # root=None -- this reader never sees "stale"
    if state in ("missing", "uninitialized"):
        return _decision_reply("search", args, state)
    conn = open_db_noncreating(db_path, project=args.project)
    if conn is None:
        return _decision_reply("search", args, "missing")
    if state in ("upgrade-required", "stale"):
        _decision_warn("search", args, state)

    # F5/F7 (ruling 71): status/type/area/topic/authority filtering now
    # lives INSIDE fts_ranked/vector_ranked themselves, before their own
    # cap and before RRF fusion -- no more Python-side `allowed` set
    # post-filtering an already-capped, already-fused list.
    extra_where, extra_params = build_filter_clause(args, include_project=False)
    results, contributing = _search_hits(conn, args, extra_where, extra_params)

    results = results[: args.limit]
    out = []
    for path, score in results:
        row = record_row_by_path(conn, path)
        if row is None:
            continue
        entry = {
            # F5: a link-row hit reports the REAL topic path (never its own
            # synthetic records.path), via link_topic_path -- every
            # existing p.endswith("foo.md")-shaped assertion, and for-path/
            # why (which stay topic-scoped), keep working unchanged.
            "path": row["link_topic_path"] or row["path"], "id": row["id"], "title": row["title"],
            "type": row["type"], "status": row["status"], "area": row["area"], "topic": row["topic"],
            "score": score, "snippet": snippet_for(row),
        }
        if row["type"] == "link":
            entry["matched_link_id"] = row["link_id"]
            entry["link_status"] = row["status"]
            entry["link_authority"] = row["authority"]
        fam = _record_family(conn, args.project, path)
        links_by_channel = contributing.get(fam)
        if links_by_channel:
            # Fix-round item 4 (coordinator review): emit whenever AT
            # LEAST ONE channel's own representative for this family is a
            # link row -- not only when both channels picked a link AND
            # those links differ. The old `len(...) > 1` gate silently
            # dropped real per-channel link information the moment one
            # channel's representative was the plain topic row (mixed
            # case: one channel matched a specific link, the other only
            # matched the topic's own aggregate text) -- that hit carried
            # neither contributing_link_ids nor (when the fused winner was
            # the topic) matched_link_id, even though a genuine per-link
            # match existed on one channel. Single-mode fts/vector search
            # still never reaches here (contributing stays {} -- only
            # hybrid mode populates it), so a plain link-row hit's own
            # matched_link_id (set above) is not duplicated by this field.
            entry["contributing_link_ids"] = links_by_channel
        out.append(entry)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for r in out:
            print(f"{r['score']:.4f}  {r['path']}  [{r['type']}] {r['title']}")
    conn.close()
    return 0


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------


def find_topic_row(conn, project: str, topic_id_or_slug: str):
    row = conn.execute(
        "SELECT * FROM records WHERE project=? AND type='topic' AND id=?",
        (project, topic_id_or_slug),
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT * FROM records WHERE project=? AND type='topic' AND path LIKE ?",
        (project, f"%{topic_id_or_slug}%"),
    ).fetchone()
    return row


def edges_for_topic(conn, topic_path: str) -> dict[str, list]:
    """from_ref ('TOP-xxxx/Lx') -> list of edge rows, insertion order."""
    rows = conn.execute(
        "SELECT rowid, * FROM edges WHERE topic_path=? ORDER BY rowid ASC", (topic_path,)
    ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["from_ref"], []).append(r)
    return grouped


def assumptions_for_topic(conn, topic_path: str) -> dict[str, list]:
    """link id -> list of assumption rows, insertion order."""
    rows = conn.execute(
        "SELECT rowid, * FROM assumptions WHERE topic_path=? ORDER BY rowid ASC", (topic_path,)
    ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        grouped.setdefault(r["link"], []).append(r)
    return grouped


def chain_lines(topic_row, link_rows, edges_by_from=None, assumptions_by_link=None) -> list[str]:
    edges_by_from = edges_by_from or {}
    assumptions_by_link = assumptions_by_link or {}

    current_link = None
    for lr in link_rows:
        if lr["status"] == "active":
            current_link = lr
            break
    if current_link is None and link_rows:
        current_link = link_rows[0]

    lines = []
    if current_link is not None:
        lines.append(
            f"{topic_row['id']} {topic_row['title']} — current: {current_link['link']} "
            f"({current_link['status']}, {current_link['ruling_authority'] or current_link['rationale_authority']})"
        )
    else:
        lines.append(f"{topic_row['id']} {topic_row['title']} — current: (none)")

    broken = []
    for lr in link_rows:
        head = f"  {lr['link']} {lr['date']} {lr['kind']}"
        if lr["reverses"]:
            reason = lr["reason_for_change"] or ""
            why = lr["rationale_text"] or lr["ruling_text"] or ""
            head += f"  ← reverses {lr['reverses']} ({reason}: {why})"
        else:
            if lr["ruling_text"]:
                quote = lr["ruling_text"]
                if lr["ruling_authority"] in ("owner-verbatim", "owner-ratified"):
                    quote = f'"{quote}"'
                head += f"   {quote} ({lr['ruling_authority']})"
            if lr["rationale_text"]:
                head += f" because {lr['rationale_text']} ({lr['rationale_authority']})"
        lines.append(head)

        from_ref = f"{topic_row['id']}/{lr['link']}"
        for edge in edges_by_from.get(from_ref, []):
            lines.append(f"    ↳ {edge['rel']} → {edge['to_ref']}")

        for a in assumptions_by_link.get(lr["link"], []):
            if a["status"] == "broken":
                broken.append(a)

    if broken:
        lines.append("broken assumptions:")
        for a in broken:
            since = f" (since {a['since']})" if a["since"] else ""
            lines.append(f"  {a['aid']}{since}: {a['text']}")

    return lines


def chain_json(topic_row, link_rows, edges_by_from, assumptions_by_link) -> dict:
    """The `chain --json` / `for-path --json` per-topic payload, shared so
    both commands emit the same shape."""
    current_link = None
    for lr in link_rows:
        if lr["status"] == "active":
            current_link = lr["link"]
            break

    links_out = []
    broken_assumptions = []
    for lr in link_rows:
        d = dict(lr)
        if d.get("invariant"):
            d["invariant"] = json.loads(d["invariant"])
        from_ref = f"{topic_row['id']}/{lr['link']}"
        d["edges"] = [{"rel": e["rel"], "to": e["to_ref"]} for e in edges_by_from.get(from_ref, [])]
        assumptions = assumptions_by_link.get(lr["link"], [])
        d["assumptions"] = [
            {"aid": a["aid"], "text": a["text"], "status": a["status"], "since": a["since"]}
            for a in assumptions
        ]
        for a in assumptions:
            if a["status"] == "broken":
                broken_assumptions.append(
                    {"aid": a["aid"], "link": lr["link"], "text": a["text"], "since": a["since"]}
                )
        links_out.append(d)

    return {
        "id": topic_row["id"],
        "title": topic_row["title"],
        "current": current_link,
        "links": links_out,
        "broken_assumptions": broken_assumptions,
    }


def cmd_chain(args) -> int:
    db_path = resolve_db_path(args)
    state = decision_index_state(db_path, args.project)   # root=None -- this reader never sees "stale"
    if state in ("missing", "uninitialized"):
        return _decision_reply("chain", args, state)
    conn = open_db_noncreating(db_path, project=args.project)
    if conn is None:
        return _decision_reply("chain", args, "missing")
    if state in ("upgrade-required", "stale"):
        _decision_warn("chain", args, state)
    topic_row = find_topic_row(conn, args.project, args.topic)
    if topic_row is None:
        print(f"no topic matching {args.topic!r}", file=sys.stderr)
        return 1
    link_rows = conn.execute(
        "SELECT * FROM links WHERE topic_path=? ORDER BY seq ASC", (topic_row["path"],)
    ).fetchall()
    edges_by_from = edges_for_topic(conn, topic_row["path"])
    assumptions_by_link = assumptions_for_topic(conn, topic_row["path"])

    if args.json:
        print(json.dumps(chain_json(topic_row, link_rows, edges_by_from, assumptions_by_link), indent=2))
    else:
        for line in chain_lines(topic_row, link_rows, edges_by_from, assumptions_by_link):
            print(line)
    conn.close()
    return 0


# ---------------------------------------------------------------------------
# for-path
# ---------------------------------------------------------------------------


def code_ref_is_named(code_ref: str) -> bool:
    """True iff `code_ref` names an actual path once its `#symbol`
    fragment is stripped -- an empty entry (`""`) or a fragment-only entry
    (`"#Foo"`) names nothing. Shared by code_ref_matches's degenerate-empty
    guard below and memlint's code_refs validation, so "what counts as a
    real code_ref" is expressed in exactly one place."""
    return bool(code_ref.split("#", 1)[0].rstrip("/"))


def code_ref_matches(file_path: str, code_ref: str) -> bool:
    """The one path-matching helper every governance lookup (code_refs,
    concept implemented_by/tested_by, drift's `allowed` exemption) shares
    (F4). Segment-aware: a directory `code_ref` only contains a file
    directly under it (a `/`-boundary check), never merely sharing a
    string prefix -- `src/foo.py.bak` is not `src/foo.py`, and
    `src/core2/x.py` is not under `src/core`. Both sides are
    `rstrip("/")`d first, so a `code_ref` authored with a trailing slash
    still matches. `#symbol` fragments are stripped before comparison,
    unaffected by this fix. An empty or fragment-only `code_ref` names no
    path and matches nothing -- without this guard the directory check
    degenerates to `fp.startswith("/")`, matching every ABSOLUTE path
    (for-path receives absolute hook-payload paths)."""
    if not code_ref_is_named(code_ref):
        return False
    ref_path = code_ref.split("#", 1)[0].rstrip("/")
    fp = file_path.rstrip("/")
    if fp == ref_path:
        return True
    if fp.startswith(ref_path + "/") or ref_path.startswith(fp + "/"):
        return True
    if fnmatch.fnmatch(fp, ref_path):
        return True
    return False


def topic_matches_for_path(conn, project: str, file_path: str) -> list:
    rows = conn.execute(
        "SELECT * FROM records WHERE project=? AND type='topic'", (project,)
    ).fetchall()
    matches = []
    for row in rows:
        code_refs = json.loads(row["code_refs"] or "[]")
        if any(code_ref_matches(file_path, ref) for ref in code_refs):
            matches.append(row)
    return matches


def concept_matches_for_path(conn, project: str, file_path: str) -> list:
    """docs/SCHEMA.md.1 addendum SS4: concepts whose implemented_by/tested_by
    matches file_path, by the same prefix/glob rule as code_refs."""
    path_rows = conn.execute(
        "SELECT * FROM concept_paths WHERE project=?", (project,)
    ).fetchall()
    matched_ids: list[str] = []
    seen = set()
    for pr in path_rows:
        if pr["concept_id"] in seen:
            continue
        if code_ref_matches(file_path, pr["path"]):
            seen.add(pr["concept_id"])
            matched_ids.append(pr["concept_id"])
    if not matched_ids:
        return []
    placeholders = ",".join("?" * len(matched_ids))
    rows = conn.execute(
        f"SELECT * FROM concepts WHERE project=? AND id IN ({placeholders})",
        (project, *matched_ids),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[cid] for cid in matched_ids if cid in by_id]


def fragment_matches_symbol(frag: str, symbol: str, qualified_name: str) -> bool:
    """The single acceptance rule for a "#symbol" fragment against one
    chunk (finding 4): exact bare-symbol match, exact full-qualified-name
    match, or a qualified suffix match (frag written as a bare member name
    or a partial dotted suffix, e.g. "outerFunc" or "Middle.outerFunc"
    matching qualified_name "Outer.Middle.outerFunc"). This is the ONE
    place this rule is expressed -- code-search's per-hit concept
    attachment (concept_matches_for_chunk, below) and memlint's #symbol
    vocabulary check (fragment_declared_in_text, and memlint.py's
    lint_concept through it) both call this instead of each encoding their
    own copy, so a fragment written qualified (e.g. "Outer.outerFunc")
    validates identically on both surfaces (reviewer finding: they used to
    disagree -- attachment accepted it, lint rejected it)."""
    return frag == symbol or frag == qualified_name or qualified_name.endswith("." + frag)


def fragment_declared_in_text(frag: str, text: str, rel_path: str) -> bool:
    """memlint's #symbol vocabulary check (memlint.py's lint_concept): is
    `frag` a symbol actually DECLARED in `text`?

    Dispatch is fully generic (Anatomy M1 fix wave, I3): the file's
    language comes from chunkers.lang_for_path(rel_path), and the answer
    comes from that backend's own `declared_symbols(text)` -- a uniform
    part of the registry contract, alongside `chunk_file`. There is no
    per-language branch here: adding a third language means adding a
    LANGUAGE_TABLE row with a backend that exposes declared_symbols, and
    this check follows for free.

    `rel_path` is REQUIRED (Task 6 drops the old "x.swift" default, which
    silently ran every unrecognized/extensionless path through the Swift
    lexer -- the bug that made `why`'s fallback resolve a Swift symbol but
    never a Python one). Resolution, same order lang_for_source_file uses
    elsewhere: extension first (chunkers.lang_for_path); only when that is
    None AND rel_path has no extension at all is text's first line sniffed
    for a shebang (chunkers.lang_for_shebang) -- an extension that simply
    doesn't match any LANGUAGE_TABLE row (e.g. ".txt") never falls through
    to the shebang guess. Still unresolved -> return False: no language
    means no vocabulary to check against, not a crash.

    Each backend returns `(symbol, qualified_name)` pairs -- including its
    container type names (Swift's class/struct/enum/protocol/extension/
    actor, Python's classes), since a #symbol fragment may name the type
    itself rather than a member. The pairs are evaluated with the SAME
    fragment_matches_symbol predicate code-search's per-hit concept
    attachment uses, so a fragment written qualified (e.g.
    "Outer.outerFunc") validates identically on both surfaces."""
    lang = chunkers.lang_for_path(rel_path)
    if lang is None and not chunkers.extension_of(rel_path):
        lang = chunkers.lang_for_shebang(text.split("\n", 1)[0])
    if lang is None:
        return False
    try:
        backend = chunkers.get_chunker(lang)
        pairs = backend.declared_symbols(text)
    except Exception:
        # Fail open, like every other chunker call site: a backend that
        # cannot answer must not turn a lint into a crash.
        return False
    return any(
        fragment_matches_symbol(frag, symbol, qualified_name)
        for symbol, qualified_name in pairs
    )


def concept_matches_for_chunk(
    conn, project: str, file_path: str, symbol: str, qualified_name: str
) -> list:
    """Finding 4: code-search's per-hit concept attachment must prefer a
    SYMBOL-level implemented_by/tested_by match over a file-level one --
    concept_matches_for_path (used by `for-path`/`unmapped`, which are
    file-level by design and stay exactly as they are) ignores any
    "#symbol" fragment entirely, so two concepts each claiming a
    different symbol in the same file would both match every chunk in it.

    A concept_paths row whose path carries a "#symbol" fragment only
    matches a chunk whose own symbol or qualified_name equals that
    fragment, or whose qualified_name ends with ".<fragment>" (so a
    fragment written as the bare member name, e.g. "outerFunc", still
    matches "Outer.outerFunc"). A row with no fragment (file-level) still
    matches every chunk in the file, same as concept_matches_for_path.
    Symbol-level matches are returned before file-level ones, so a caller
    that only wants the single best match (matches[0]) prefers the more
    specific one."""
    path_rows = conn.execute(
        "SELECT * FROM concept_paths WHERE project=?", (project,)
    ).fetchall()
    symbol_ids: list[str] = []
    file_ids: list[str] = []
    seen_symbol: set = set()
    seen_file: set = set()
    for pr in path_rows:
        ref_path, _, frag = pr["path"].partition("#")
        if not code_ref_matches(file_path, ref_path):
            continue
        cid = pr["concept_id"]
        if frag:
            if fragment_matches_symbol(frag, symbol, qualified_name):
                if cid not in seen_symbol:
                    seen_symbol.add(cid)
                    symbol_ids.append(cid)
        else:
            if cid not in seen_file:
                seen_file.add(cid)
                file_ids.append(cid)
    matched_ids = symbol_ids + [cid for cid in file_ids if cid not in seen_symbol]
    if not matched_ids:
        return []
    placeholders = ",".join("?" * len(matched_ids))
    rows = conn.execute(
        f"SELECT * FROM concepts WHERE project=? AND id IN ({placeholders})",
        (project, *matched_ids),
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[cid] for cid in matched_ids if cid in by_id]


def governed_topic_rows(conn, project: str, concept_row) -> list:
    ids = json.loads(concept_row["governed_by"] or "[]")
    rows = []
    for tid in ids:
        row = conn.execute(
            "SELECT * FROM records WHERE project=? AND type='topic' AND id=?", (project, tid)
        ).fetchone()
        if row:
            rows.append(row)
    return rows


def topic_chain_json(conn, topic_row) -> dict:
    link_rows = conn.execute(
        "SELECT * FROM links WHERE topic_path=? ORDER BY seq ASC", (topic_row["path"],)
    ).fetchall()
    return chain_json(
        topic_row, link_rows,
        edges_for_topic(conn, topic_row["path"]),
        assumptions_for_topic(conn, topic_row["path"]),
    )


def print_topic_chain(conn, topic_row) -> None:
    link_rows = conn.execute(
        "SELECT * FROM links WHERE topic_path=? ORDER BY seq ASC", (topic_row["path"],)
    ).fetchall()
    for line in chain_lines(
        topic_row, link_rows,
        edges_for_topic(conn, topic_row["path"]),
        assumptions_for_topic(conn, topic_row["path"]),
    ):
        print(line)


def concept_json(conn, project: str, concept_row) -> dict:
    return {
        "id": concept_row["id"],
        "kind": "concept",
        "title": concept_row["title"],
        "owner_boundary": concept_row["owner_boundary"],
        "implemented_by": json.loads(concept_row["implemented_by"] or "[]"),
        "tested_by": json.loads(concept_row["tested_by"] or "[]"),
        "governed_by": [
            topic_chain_json(conn, t) for t in governed_topic_rows(conn, project, concept_row)
        ],
    }


def cmd_for_path(args) -> int:
    """F1 (ruling 68): 2 stays argparse's own reserved usage-error code; 3 =
    missing/uninitialized (collapsed -- no positive match worth attempting,
    including the TOCTOU race where the file vanishes between the state
    check and the open below); 4 = index-error (a schema a migration guard
    should already have fixed but didn't -- ruling 65's belt-and-suspenders
    catch). upgrade-required/current/stale all proceed normally; a positive
    match off a non-current index stays usable."""
    db_path = resolve_db_path(args)
    state = decision_index_state(db_path, args.project)
    if state in ("missing", "uninitialized"):
        print(json.dumps([], indent=2) if args.json else "no topics reference this path")
        return 3
    try:
        conn = open_db_noncreating(db_path, project=args.project)
        if conn is None:
            print(json.dumps([], indent=2) if args.json else "no topics reference this path")
            return 3
        if state in ("upgrade-required", "stale"):
            _decision_warn("for-path", args, state)
        matches = topic_matches_for_path(conn, args.project, args.file_path)
        concept_matches = concept_matches_for_path(conn, args.project, args.file_path)

        if args.json:
            out = [topic_chain_json(conn, row) for row in matches]
            out.extend(concept_json(conn, args.project, crow) for crow in concept_matches)
            print(json.dumps(out, indent=2))
        else:
            if not matches and not concept_matches:
                print("no topics reference this path")
            for row in matches:
                print_topic_chain(conn, row)
            for crow in concept_matches:
                print(f"{crow['id']} {crow['title']} — {crow['owner_boundary']}")
                for trow in governed_topic_rows(conn, args.project, crow):
                    print_topic_chain(conn, trow)
        conn.close()
        return 0
    except (sqlite3.OperationalError, IndexError):
        # A schema mismatch a migration guard should already have fixed by
        # now can still surface two ways: a raw SQL query naming a column
        # that no longer exists (sqlite3.OperationalError), or a `SELECT *`
        # + row["col"] access on a row whose columns were renamed out from
        # under it (sqlite3.Row raises IndexError, not OperationalError, for
        # a missing key) -- both mean the same thing here: fail open, never
        # crash a hook-facing reader.
        print(json.dumps([], indent=2) if args.json else "no topics reference this path")
        return 4


# ---------------------------------------------------------------------------
# code-tree helpers shared by `why` (symbol resolution) and `drift` (invariant checks)
# ---------------------------------------------------------------------------

def is_binary_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    return b"\0" in chunk


def iter_code_files(code_root: Path):
    """Every non-binary file under `code_root`, noise dirs pruned (Task 6:
    chunkers.UNIVERSAL_SKIP_DIRS -- the same set CODE_SKIP_DIR_NAMES
    aliases below -- not a narrower/older SKIP_DIR_NAMES). Deliberately
    yields EVERY language, not just wired ones: `drift`'s
    pattern-absent/no-bypass/single-definition invariants (check_invariant,
    below) scan non-language files too (binding point 7) -- the language
    filter lives in resolve_symbol_to_path's own loop, not here."""
    for dirpath, dirnames, filenames in os.walk(code_root):
        dirnames[:] = [d for d in dirnames if d not in chunkers.UNIVERSAL_SKIP_DIRS]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if is_binary_file(fpath):
                continue
            yield fpath


def _resolve_symbol_via_code_index(code_root: Path, symbol: str, project: str) -> tuple[str, str] | None:
    """Finding 7 fast path for resolve_symbol_to_path, below: consult the
    code index's `chunks` table (member symbols only -- container names
    like class/struct/enum/protocol/extension/actor are never chunks
    themselves, see chunk_source's own docstring -- so a miss here is
    never conclusive; the caller always falls back to the full lexer
    scan, which does see container names too). Deliberately never
    consults `--db`: on `why`, that flag means the DECISION db override
    (exactly the confusion --decision-db exists to prevent for
    code-search's own concept attachment) -- only the default
    "<project>-code.sqlite" path is ever read here.

    Anatomy M2a Task 5: multi-root aware (Task 4's code_index_report, not a
    single code_meta row) and NEVER heals -- unlike code-search, `why` only
    ever gets one shot at an answer, so a stale index must not be silently
    trusted for it, and repairing it here would make a `why` call mutate
    the index as a side effect, which nothing about `why` implies should
    happen. Precisely: this function itself never heals and never writes a
    chunk, status, or meta row; but code_index_report (called below, the
    same preflight code-search's own heal decision reads) may still commit
    a stored file's mtime/size cache refresh when a drifted-looking row's
    content turns out unchanged (cache bookkeeping, not a repair -- see
    _root_report's own docstring). Untrusted (returns None, sending the caller to the disk
    scan) whenever: the report's overall state is `stale` (some root's
    content has drifted -- a state-wide caution, since a chunk this SAME
    root reports as current could still be a false hit once ANY root in
    the project has unindexed drift and the caller cannot tell which chunk
    came from which), or `code_root` (resolved) is not one of the report's
    own recorded roots at all (an index that never saw this tree, or one
    that predates it). A `degraded` report (not-indexed / missing-root
    rows elsewhere, no drifted content) is still trusted for a chunk that
    IS present, same as before this task.

    Returns (code_root, path) -- the resolved root alongside the chunk's
    stored relative path, so a multi-root caller (Task 6) can tell which
    tree the match came from; resolve_symbol_to_path (immediately below)
    unpacks the tuple and returns just the path for now.

    Fix-wave item 3: never calls open_code_db directly on an
    older-than-current-schema db. open_code_db's schema check doubles as a
    REBUILD trigger (it drops and recreates the derived tables as a side
    effect of opening); `why` only gets one shot per call and never heals
    (see above), so if this were the thing that triggered that rebuild, a
    bare-symbol `why` right after an engine upgrade would silently empty
    the whole index and leave it stale until the next code-search -- worse
    than just missing the fast path once. So the schema version is read
    first with a plain, read-only connection; anything below current (or
    the table missing entirely) sends the caller to the disk scan WITHOUT
    opening (and thus without rebuilding) the db at all. The next
    code-search still finds it stale and heals it normally."""
    code_db_path = (
        Path(os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum")))
        / f"{project}-code.sqlite"
    )
    if not code_db_path.exists():
        return None
    # sqlite3.connect() is lazy -- a corrupt/non-database file never raises
    # here, only on the first real read below, so BOTH the connect and the
    # version read live inside this one try/except (a bare try/finally
    # around just the read would let that DatabaseError escape uncaught,
    # crashing `why` on a corrupt db exactly like the bug this function
    # exists to prevent).
    try:
        raw = sqlite3.connect(str(code_db_path))
        try:
            version = code_schema_version(raw)
        finally:
            raw.close()
    except sqlite3.DatabaseError:
        return None
    if version < CODE_SCHEMA_VERSION:
        return None
    try:
        conn = open_code_db(code_db_path)
    except sqlite3.DatabaseError:
        return None
    try:
        report = code_index_report(conn, project)
        if report["state"] == "stale":
            return None
        try:
            root_s = str(code_root.resolve())
        except OSError:
            return None
        if root_s not in {r["code_root"] for r in report["roots"]}:
            return None
        rows = conn.execute(
            "SELECT path, symbol, qualified_name FROM chunks WHERE project=? AND code_root=? ORDER BY path",
            (project, root_s),
        ).fetchall()
        for r in rows:
            if fragment_matches_symbol(symbol, r["symbol"], r["qualified_name"]):
                return (root_s, r["path"])
        return None
    finally:
        conn.close()


def resolve_symbol_to_path(code_root: Path, symbol: str, project: str | None = None) -> str | None:
    """Finding 7: resolve a bare --code-root symbol to its defining file
    for `why`, consuming the SAME per-language symbol vocabulary memlint
    and code-search attachment already agree on -- each backend's own
    declared_symbols
    (via fragment_declared_in_text, so a QUALIFIED symbol like
    "Outer.outerFunc" also resolves) -- not the old from-scratch regex
    (`func|class|struct|enum|let|var` only, missing init, subscript,
    operators, backtick-quoted names, and the actor/protocol/extension
    container keywords entirely).

    Tries the code index first (_resolve_symbol_via_code_index) when
    `project` is given, for speed; always falls back to scanning code_root
    directly -- iter_code_files prunes only chunkers.UNIVERSAL_SKIP_DIRS
    (the universal noise set -- see that set's own docstring for the
    member list and rationale), never a language's own skip_dirs, so `why`
    stays able to resolve a symbol declared under Tests/, a behavior
    change nobody asked for -- so a missing/stale/member-only-index miss
    never regresses a resolution the old regex-based version could already
    make.

    Task 6: dispatch is language-aware, not Swift-only. Each candidate
    file's language is resolved with lang_for_source_file (extension
    first, then a shebang sniff for an extensionless file) BEFORE it is
    even read -- a file with no resolvable language (an extension with no
    LANGUAGE_TABLE row at all, checked against the registry, not against
    this project's wired subset; or a non-language file drift's
    iter_code_files also walks) is skipped outright, never sent through
    the Swift lexer as the old default rel_path="x.swift" silently did
    (the bug: a `.py` file's `def` syntax never matched Swift's grammar,
    so a bare Python symbol could never resolve here at all)."""
    if project:
        resolved = _resolve_symbol_via_code_index(code_root, symbol, project)
        if resolved is not None:
            _root_s, path = resolved
            return path
    for fpath in sorted(iter_code_files(code_root)):
        if lang_for_source_file(fpath) is None:
            continue
        try:
            rel = str(fpath.relative_to(code_root))
        except ValueError:
            rel = str(fpath)
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if fragment_declared_in_text(symbol, text, rel):
            return rel
    return None


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------


def cmd_why(args) -> int:
    """docs/SCHEMA.md.1 addendum SS5: path -> concept -> governed_by topics,
    printed newest-first INCLUDING declined links (the reviewer's "why is
    this code strange?" view)."""
    target = args.symbol_or_path
    if "/" in target:
        file_path = target
    else:
        if not args.code_root:
            print(
                f"why: bare symbol {target!r} needs --code-root to resolve its defining file",
                file=sys.stderr,
            )
            return 2
        resolved = resolve_symbol_to_path(Path(args.code_root).resolve(), target, project=args.project)
        if resolved is None:
            print(f"why: no definition of {target!r} found under {args.code_root}", file=sys.stderr)
            return 1
        file_path = resolved

    db_path = resolve_db_path(args)
    state = decision_index_state(db_path, args.project)   # root=None -- this reader never sees "stale"
    if state in ("missing", "uninitialized"):
        return _decision_reply("why", args, state)
    conn = open_db_noncreating(db_path, project=args.project)
    if conn is None:
        return _decision_reply("why", args, "missing")
    if state in ("upgrade-required", "stale"):
        _decision_warn("why", args, state)
    concept_matches = concept_matches_for_path(conn, args.project, file_path)

    if args.json:
        out = [concept_json(conn, args.project, c) for c in concept_matches]
        print(json.dumps(out, indent=2))
    else:
        if not concept_matches:
            print(f"no concept claims {file_path!r}")
        for crow in concept_matches:
            print(f"{crow['id']} {crow['title']} — {crow['owner_boundary']}")
            for trow in governed_topic_rows(conn, args.project, crow):
                print_topic_chain(conn, trow)
    conn.close()
    return 0


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


# F3 (external-fix round, coordinator ruling 70; HOLD_ELIGIBLE_AUTHORITIES
# widened by ruling 76, which overrides the brief's original exclusion). The
# trust model's three enforcement classes. docs/SCHEMA.md §4's CONSTRAINT
# tier is exactly owner-verbatim/owner-ratified; HOLD is reviewer-finding,
# code-derived, OR agent-inference, but ONLY with validated evidence in every
# case -- eligibility is uniform across all three HOLD-eligible authorities,
# never a bare non-empty list promoting anything by itself (still Codex's
# specific correction over a flatter authority-OR-evidence rule: an
# agent-inference link with EMPTY evidence stays CONTEXT, same as any other
# HOLD-eligible authority with no validated evidence).
CONSTRAINT_AUTHORITIES = {"owner-verbatim", "owner-ratified"}
HOLD_ELIGIBLE_AUTHORITIES = {"reviewer-finding", "code-derived", "agent-inference"}


def validated_evidence_list(raw) -> list[str]:
    """The parsed-evidence-shape core shared by drift's classifier
    (_validated_evidence below) and memlint's own check (memlint.py imports
    this directly): an `evidence` value is "validated" only when it is an
    actual list, and only its non-blank string entries count. None, a bare
    scalar (a string is NOT a list of one string), a dict, and a list of
    non-strings/blank-strings all normalize to [] (or drop those entries);
    a mixed list keeps only its non-blank string members. Ruling 70:
    evidence must be validated content, never merely tested for
    truthiness."""
    if not isinstance(raw, list):
        return []
    return [e.strip() for e in raw if isinstance(e, str) and e.strip()]


def _validated_evidence(link_row) -> list[str]:
    """Parses `evidence` (a `links` row's own JSON-encoded column) and
    hands it to validated_evidence_list -- the same core memlint uses
    directly on the raw YAML value."""
    try:
        raw = json.loads(link_row["evidence"] or "[]")
    except (TypeError, ValueError):
        return []
    return validated_evidence_list(raw)


def invariant_enforcement_class(link_row) -> str:
    """"constraint" | "hold" | "context" -- ruling 70's four-class rule
    (three outcomes; the fourth, "no invariant at all", never reaches this
    function; a fifth outcome, "provisional -- reported for revalidation
    without enforcement," is ruling 74's and lives in cmd_drift's own
    provisional branch, never here -- this function is never called for a
    provisional row). "constraint": active owner-verbatim/owner-ratified --
    always enforced, a violation always fails the run. "hold": active
    reviewer-finding/code-derived/agent-inference (ruling 76) WITH
    validated evidence -- reported, only fails the exit under
    --strict-holds. "context": everything else -- a non-active status, or a
    HOLD-eligible authority (any of the three) with no validated evidence."""
    if link_row["status"] != "active":
        return "context"
    authority = link_row["ruling_authority"]
    if authority in CONSTRAINT_AUTHORITIES:
        return "constraint"
    if authority in HOLD_ELIGIBLE_AUTHORITIES and _validated_evidence(link_row):
        return "hold"
    return "context"


class InvariantSkipped(Exception):
    """check_invariant refuses this invariant outright (bad kind/regex/
    scope) rather than silently reporting no drift; cmd_drift catches this
    per-entry and prints one distinct "skipped" line instead of a
    traceback or a false "clean" result."""


def links_with_active_invariant(conn, project: str):
    """(link row, invariant dict) for every active OR provisional link that
    carries one (ruling 74 widens this from active-only: a provisional
    invariant must be visible to `drift` for revalidation, never silently
    absent). cmd_drift itself is what tells the two statuses apart -- a
    provisional row goes straight to the `revalidate` bucket, never through
    invariant_enforcement_class at all."""
    rows = conn.execute(
        "SELECT * FROM links WHERE project=? AND status IN ('active','provisional') "
        "AND invariant IS NOT NULL",
        (project,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            inv = json.loads(r["invariant"])
        except (TypeError, ValueError):
            continue
        if inv:
            out.append((r, inv))
    return out


def check_invariant(code_root: Path, invariant: dict) -> list[str]:
    """Run one docs/SCHEMA.md.1 SS3 invariant against code_root. Returns the
    drift evidence lines -- empty means the invariant holds. F3 (ruling 70):
    every way this can be unevaluable is a named InvariantSkipped instead of
    a silent `return []` or an uncaught traceback -- an unknown `kind`, an
    invalid regex, or a `must-call` with no `scope`. A `single-definition`
    with zero matches is still a real violation ("defined nowhere"), not a
    skip."""
    kind = invariant.get("kind")
    if kind not in INVARIANT_KINDS:
        raise InvariantSkipped(f"unknown kind {kind!r}")
    try:
        pattern = re.compile(invariant.get("pattern") or "")
    except re.error as exc:
        raise InvariantSkipped(f"invalid regex: {exc}") from exc
    allowed = invariant.get("allowed") or []

    if kind in ("pattern-absent", "no-bypass"):
        hits = []
        for fpath in sorted(iter_code_files(code_root)):
            rel = str(fpath.relative_to(code_root))
            if any(code_ref_matches(rel, a) for a in allowed):
                continue
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(f"{rel}:{i}")
        return hits

    if kind == "single-definition":
        all_hits = []
        for fpath in sorted(iter_code_files(code_root)):
            rel = str(fpath.relative_to(code_root))
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    all_hits.append(f"{rel}:{i}")
        if not all_hits:
            return ["<no definition found>"]
        return [] if len(all_hits) == 1 else all_hits

    # kind == "must-call" (the only remaining member of INVARIANT_KINDS)
    scope = invariant.get("scope")
    if not scope:
        raise InvariantSkipped("missing scope")
    matched = sorted(code_root.glob(scope))
    if not matched:
        # Coordinator fix: a scope glob matching zero files used to fall
        # through the empty loop below and return [] -- a vacuous pass
        # indistinguishable from "every matched file makes the required
        # call". A scope that matches nothing is unevaluable, not clean.
        raise InvariantSkipped(f"must-call scope matches no files: {scope}")
    missing = []
    for fpath in matched:
        if not fpath.is_file() or is_binary_file(fpath):
            continue
        rel = str(fpath.relative_to(code_root))
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        if not pattern.search(text):
            missing.append(rel)
    return missing


def _format_drift_line(prefix: str, r: dict) -> str:
    label = f"{r['topic']}/{r['link']}"
    joined = ", ".join(r["hits"])
    if r["kind"] in ("pattern-absent", "no-bypass"):
        return f"{prefix}: {label} — {len(r['hits'])} hits outside allowed: {joined}"
    if r["kind"] == "single-definition":
        if r["hits"] == ["<no definition found>"]:
            return f"{prefix}: {label} — expected exactly 1 definition, found 0"
        return f"{prefix}: {label} — expected exactly 1 definition, found {len(r['hits'])}: {joined}"
    if r["kind"] == "must-call":
        return f"{prefix}: {label} — {len(r['hits'])} file(s) missing required call: {joined}"
    return f"{prefix}: {label} — {len(r['hits'])} hits: {joined}"


def cmd_drift(args) -> int:
    """F3 (ruling 70, HOLD_ELIGIBLE_AUTHORITIES widened by ruling 76):
    three enforcement buckets, not one flat list -- an active
    owner-verbatim/owner-ratified invariant is a CONSTRAINT violation
    (always fails the run); an active reviewer-finding/code-derived/
    agent-inference invariant with validated evidence is a HOLD violation
    (reported, only fails the exit under --strict-holds); everything else
    (non-active status, or a HOLD-eligible authority with no validated
    evidence) is CONTEXT -- named `skipped`, never silently enforced.
    check_invariant's own InvariantSkipped (bad kind/regex/scope/empty
    must-call scope) is caught per-entry and named too, never a traceback.
    Ruling 74 adds a fourth, orthogonal bucket: a `provisional` link
    carrying an invariant is never classified or checked at all -- it goes
    straight to `revalidate`, reported for live revalidation, never a
    failure even under --strict-holds."""
    db_path = resolve_db_path(args)
    # --code-root points at CODE, not the decision store's own markdown
    # tree, so this reader cannot compute "stale" either -- root stays None,
    # same as search/chain/why.
    state = decision_index_state(db_path, args.project)
    if state in ("missing", "uninitialized"):
        return _decision_reply("drift", args, state)
    conn = open_db_noncreating(db_path, project=args.project)
    if conn is None:
        return _decision_reply("drift", args, "missing")
    if state in ("upgrade-required", "stale"):
        _decision_warn("drift", args, state)
    code_root = Path(args.code_root).resolve()
    entries = links_with_active_invariant(conn, args.project)
    conn.close()

    violations, hold_violations, skipped, revalidate = [], [], [], []
    for link_row, invariant in entries:
        label = f"{link_row['topic_id']}/{link_row['link']}"
        if link_row["status"] == "provisional":
            # Ruling 74: reported for live revalidation, never checked,
            # never enforced -- not routed through invariant_enforcement_class
            # (which is never called for a non-active/provisional row) or
            # check_invariant at all.
            revalidate.append({"topic": link_row["topic_id"], "link": link_row["link"],
                                "kind": invariant.get("kind")})
            continue
        eclass = invariant_enforcement_class(link_row)
        if eclass == "context":
            reason = "authority" if link_row["status"] == "active" else f"status={link_row['status']}"
            skipped.append({"link": label, "reason": reason})
            continue
        try:
            hits = check_invariant(code_root, invariant)
        except InvariantSkipped as exc:
            skipped.append({"link": label, "reason": str(exc)})
            continue
        if not hits:
            continue
        entry = {"topic": link_row["topic_id"], "link": link_row["link"],
                 "kind": invariant.get("kind"), "hits": hits}
        (violations if eclass == "constraint" else hold_violations).append(entry)

    strict = getattr(args, "strict_holds", False)
    if args.json:
        print(json.dumps({"violations": violations, "hold_violations": hold_violations,
                           "skipped": skipped, "revalidate": revalidate}, indent=2))
    else:
        if not violations and not hold_violations:
            print("drift: no invariants violated")
        for r in violations:
            print(_format_drift_line("DRIFT", r))
        for r in hold_violations:
            print(_format_drift_line("HOLD", r))
        for s in skipped:
            print(f"drift: skipped {s['link']} ({s['reason']})")
        for r in revalidate:
            print(f"revalidate (provisional): {r['topic']}/{r['link']} {r['kind']}")
    return 1 if (violations or (strict and hold_violations)) else 0


# ---------------------------------------------------------------------------
# unmapped -- write-side reminder hooks' engine addition (docs/DESIGN.md)
# ---------------------------------------------------------------------------


def _index_has_drift(conn: sqlite3.Connection, root: Path, project: str) -> bool:
    """Same added/changed/removed-by-mtime-and-size comparison as cmd_check,
    factored out so `unmapped` can reuse it without cmd_check's printing.
    Ruling 66: a link row is never a real file on disk (walk_markdown never
    yields one), so it must never be mistaken for one that vanished --
    the NULL-aware source_path predicate (see cmd_reindex's own comment on
    the identical query) excludes every link row here; a pre-migration
    legacy row (source_path still NULL) is still counted as real."""
    existing = {
        row["path"]: (row["mtime"], row["size"])
        for row in conn.execute(
            "SELECT path, mtime, size FROM records WHERE project=? "
            "AND (source_path IS NULL OR source_path = path)",
            (project,),
        )
    }
    seen = set()
    for f in walk_markdown(root):
        path_str = str(f)
        seen.add(path_str)
        stat = f.stat()
        prev = existing.get(path_str)
        if prev is None or prev[0] != stat.st_mtime or prev[1] != stat.st_size:
            return True
    if set(existing.keys()) - seen:
        return True
    return False


def _unmapped_path_candidates(raw_path: str, code_root: Path | None) -> list[str]:
    """Candidates to try against the index's (repo-relative) code_refs /
    concept paths: the path as given, and -- when --code-root is supplied and
    the given path is absolute -- that path made relative to code_root. This
    is the engine-side fix for the multi-candidate workaround documented at
    the top of hooks/pre-edit-chain.sh (a PreToolUse/PostToolUse file_path is
    always absolute; code_refs are conventionally repo-relative)."""
    candidates = [raw_path]
    if code_root is not None:
        try:
            p = Path(raw_path)
            if p.is_absolute():
                rel = str(p.resolve().relative_to(code_root))
                if rel not in candidates:
                    candidates.append(rel)
        except (OSError, ValueError):
            pass
    return candidates


def _unmapped_display_path(raw_path: str, code_root: Path | None) -> str:
    """The path string reported back for one PATH argument: relative to
    --code-root when that's resolvable, else the path exactly as given."""
    if code_root is not None:
        try:
            p = Path(raw_path)
            if p.is_absolute():
                return str(p.resolve().relative_to(code_root))
        except (OSError, ValueError):
            pass
    return raw_path


def cmd_unmapped(args) -> int:
    """`memidx.py unmapped PATH... --root R [--code-root CR] [--json]`

    For each PATH, classifies it as mapped_topic (a topic's code_refs
    references it), mapped_concept_only (no topic does, but a concept's
    implemented_by/tested_by does), or unmapped (neither) -- purely by
    querying the existing index, one lookup per candidate per path (O(paths),
    never a code-tree walk). Never imports fastembed.

    F1 (ruling 68): `coverage_status` mirrors decision_index_state's own
    five states, collapsed for a NEGATIVE claim's purposes ("no topic covers
    this file" is untrusted off anything but a genuinely current index):
    "ok" (state == "current", queried normally), "unknown" (genuine
    unresolved drift, or any other read failure -- unchanged from before
    F1), "uninitialized" (state missing/uninitialized, collapsed -- no
    query attempted, `unmapped` self-heal never fires here), "upgrade-
    required" (state upgrade-required -- self-heal does NOT fire; that is
    the rollout's job, not an ad-hoc hook-triggered one), "index-error" (a
    sqlite3.OperationalError while reading -- ruling 65's belt-and-
    suspenders fail-open). Self-healing is now gated on state == "stale"
    (a same-generation on-disk drift decision_index_state already computed
    above -- no second walk): one `reindex --no-embed` pass, then re-check
    for drift the same way as before F1. `unmapped` is always [] whenever
    coverage_status != "ok" (a positive match found on a not-fully-current
    index is still real evidence; the *absence* of a match is what an
    unknown-freshness index must never be allowed to assert -- docs/
    DESIGN.md ruling F, "never a false gap").
    """
    root = Path(args.root).resolve()
    db_path = resolve_db_path(args)
    code_root = Path(args.code_root).resolve() if getattr(args, "code_root", None) else None

    coverage_status = "ok"
    mapped_topic: list[str] = []
    mapped_concept_only: list[str] = []
    unmapped: list[str] = []
    conn: sqlite3.Connection | None = None
    try:
        state = decision_index_state(db_path, args.project, root=root)
        if state in ("missing", "uninitialized"):
            coverage_status = "uninitialized"
        elif state == "upgrade-required":
            coverage_status = "upgrade-required"
        else:
            conn = open_db_noncreating(db_path, project=args.project)
            if conn is None:
                coverage_status = "uninitialized"
            else:
                if state == "stale":
                    reindex_ns = argparse.Namespace(
                        root=str(root), project=args.project, db=str(db_path), full=False,
                        no_embed=True, auto=True,
                    )
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        cmd_reindex(reindex_ns)
                    conn.close()
                    conn = open_db_noncreating(db_path, project=args.project)
                    if conn is not None and _index_has_drift(conn, root, args.project):
                        coverage_status = "unknown"
                if conn is not None:
                    for raw_path in args.paths:
                        candidates = _unmapped_path_candidates(raw_path, code_root)
                        display = _unmapped_display_path(raw_path, code_root)
                        topic_hit = any(topic_matches_for_path(conn, args.project, c) for c in candidates)
                        concept_hit = False
                        if not topic_hit:
                            concept_hit = any(
                                concept_matches_for_path(conn, args.project, c) for c in candidates
                            )
                        if topic_hit:
                            mapped_topic.append(display)
                        elif concept_hit:
                            mapped_concept_only.append(display)
                        elif coverage_status == "ok":
                            unmapped.append(display)
    except sqlite3.OperationalError:
        coverage_status = "index-error"
        mapped_topic, mapped_concept_only, unmapped = [], [], []
    except Exception:
        coverage_status = "unknown"
        mapped_topic, mapped_concept_only, unmapped = [], [], []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    result = {
        "mapped_topic": mapped_topic,
        "mapped_concept_only": mapped_concept_only,
        "unmapped": unmapped,
        "coverage_status": coverage_status,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"coverage_status: {coverage_status}")
        for label, paths in (
            ("mapped_topic", mapped_topic),
            ("mapped_concept_only", mapped_concept_only),
            ("unmapped", unmapped),
        ):
            print(f"{label}: {len(paths)}")
            for p in paths:
                print(f"  {p}")
    return 0 if coverage_status == "ok" else 1


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def cmd_check(args) -> int:
    root = Path(args.root).resolve()
    db_path = resolve_db_path(args)
    # The only root-less-in-this-group... exception: `check` DOES take
    # --root (the decision store's own markdown tree), so it's the one
    # reader here that can genuinely see "stale".
    state = decision_index_state(db_path, args.project, root=root)
    if state in ("missing", "uninitialized"):
        return _decision_reply("check", args, state)
    conn = open_db_noncreating(db_path, project=args.project)
    if conn is None:
        return _decision_reply("check", args, "missing")
    if state in ("upgrade-required", "stale"):
        _decision_warn("check", args, state)
    # Ruling 66: same NULL-aware source_path predicate as cmd_reindex/
    # _index_has_drift -- a link row is never a real file, never counted
    # here as added/changed/removed.
    existing = {
        row["path"]: (row["mtime"], row["size"])
        for row in conn.execute(
            "SELECT path, mtime, size FROM records WHERE project=? "
            "AND (source_path IS NULL OR source_path = path)",
            (args.project,),
        )
    }
    files = list(walk_markdown(root))
    seen = set()
    changed = []
    added = []
    for f in files:
        path_str = str(f)
        seen.add(path_str)
        stat = f.stat()
        prev = existing.get(path_str)
        if prev is None:
            added.append(path_str)
        elif prev[0] != stat.st_mtime or prev[1] != stat.st_size:
            changed.append(path_str)
    removed = sorted(set(existing.keys()) - seen)

    drift = bool(added or changed or removed)
    report = {"added": added, "changed": changed, "removed": removed, "drift": drift}
    # F5 (Codex's addition): growth-count visibility -- the real
    # source-topic count vs. the total searchable row/link/vector count, so
    # a store's index growth from link rows is visible, not hidden inside
    # one aggregate number.
    report["source_topic_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM records WHERE project=? AND (source_path IS NULL OR source_path = path)",
        (args.project,),
    ).fetchone()["n"]
    report["searchable_row_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM records WHERE project=?", (args.project,)
    ).fetchone()["n"]
    report["searchable_vector_count"] = conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings e JOIN records r ON r.path=e.path AND r.sha256=e.embed_sha "
        "WHERE e.project=?", (args.project,)
    ).fetchone()["n"]
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        if not drift:
            print("check: index is up to date")
        else:
            print(f"check: drift detected -- added={len(added)} changed={len(changed)} removed={len(removed)}")
            for p in added:
                print(f"  + {p}")
            for p in changed:
                print(f"  ~ {p}")
            for p in removed:
                print(f"  - {p}")
    conn.close()
    return 1 if drift else 0


# ---------------------------------------------------------------------------
# code index -- `code-reindex` / `code-search` (Anatomy's intent index)
#
# A completely separate SQLite DB ($MEMCONTINUUM_HOME/<project>-code.sqlite,
# see resolve_code_db_path/CODE_SCHEMA_SQL) from the markdown decision index
# above. Chunks a Swift source tree into func/init/subscript/computed-var
# units via a lexer-aware brace walker (comments, strings incl. raw/
# multiline, string-interpolation closures, and #if branches all handled --
# see chunk_source below) and serves fts/vector/hybrid search over them,
# reusing the same fts_escape/pack_vector/unpack_vector/cosine/
# compute_embeddings/compute_query_embedding helpers as the markdown path so
# `--mode fts` never imports fastembed here either.
#
# class/struct/enum/protocol/extension are NEVER chunks themselves -- they
# only qualify names (an enclosing-type stack, `types` below) so e.g. a
# method inside `extension RuleEngine { func f() {} }` is indexed as
# `RuleEngine.f`.
# ---------------------------------------------------------------------------

# Task 5 rewire: a thin view over chunkers.LANGUAGE_TABLE, not a second
# hand-maintained extension map (Task 3 reviewer finding -- the registry is
# now the single source of truth). Kept only for back-compat with anything
# still reading LANG_EXTENSIONS directly; dispatch itself goes through
# chunkers.lang_for_path (see lang_for_source_file / iter_code_source_files
# below).
LANG_EXTENSIONS = {lang: row["extensions"] for lang, row in chunkers.LANGUAGE_TABLE.items()}

# Task 7 (superseded by Task 6): this is the GLOBAL skip set ONLY --
# directory names that are always noise regardless of which languages are
# wired, or of whether any language is wired at all. Language-specific
# noise (swift's Tests/Resources, python's build/dist) lives on each
# LANGUAGE_TABLE row's "skip_dirs" key instead, so a Swift-only project's
# own build/ or dist/ output is never pruned by a rule meant for Python
# virtualenvs, and vice versa. This global set is what
# iter_code_source_files prunes for every walk; per-language sets are
# applied per FILE, to that language's own files only (fix wave C1 -- see
# chunkers.common_skip_dirs).
#
# Task 6: an ALIAS of chunkers.UNIVERSAL_SKIP_DIRS, not a second
# hand-maintained set -- iter_code_files (the `why`-fallback/`drift` walker,
# which has no notion of "wired languages" at all) prunes the very same
# set. One definition, two names kept for their own call sites' history.
CODE_SKIP_DIR_NAMES = chunkers.UNIVERSAL_SKIP_DIRS

CODE_SCHEMA_VERSION = 2
# Fix-wave item 8: code_project belongs here too -- the v1->v2 preserving
# path (_preserved_code_config, below) reads the OLD code_meta before this
# drop runs and reinserts code_project rows from it, but a rebuild that
# left an old code_project table standing would carry forward whatever
# stale langs/embedding_mode rows an older engine wrote instead of the
# reseeded ones. Dropping it here and letting it get recreated by
# CODE_SCHEMA_SQL, then reseeded from `keep`, is the same "rebuilt empty,
# non-derived facts carried over" contract the other derived tables get.
CODE_TABLES = ("chunks", "fts", "embeddings", "file_sha", "code_meta", "code_schema", "code_project")

CODE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS code_project (
  project TEXT PRIMARY KEY,
  langs TEXT,
  embedding_mode TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  project TEXT NOT NULL,
  code_root TEXT NOT NULL,
  lang TEXT NOT NULL,
  kind TEXT NOT NULL,
  symbol TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  signature TEXT,
  doc TEXT,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON chunks(project, path);
CREATE INDEX IF NOT EXISTS idx_code_chunks_qname ON chunks(project, qualified_name);
CREATE INDEX IF NOT EXISTS idx_code_chunks_root_path ON chunks(project, code_root, path);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  qualified_name, split_tokens, signature, doc, body,
  tokenize = "unicode61 tokenchars '_'"
);

CREATE TABLE IF NOT EXISTS embeddings (
  chunk_id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS file_sha (
  path TEXT NOT NULL,
  project TEXT NOT NULL,
  code_root TEXT NOT NULL,
  sha256 TEXT,
  mtime REAL,
  size INTEGER,
  gap_count INTEGER NOT NULL DEFAULT 0,
  chunker_version TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  reason TEXT,
  attempt_key TEXT,
  PRIMARY KEY (project, code_root, path)
);

CREATE TABLE IF NOT EXISTS code_meta (
  project TEXT NOT NULL,
  code_root TEXT NOT NULL,
  last_indexed_at REAL,
  head_sha TEXT,
  PRIMARY KEY (project, code_root)
);

CREATE TABLE IF NOT EXISTS code_schema (
  version INTEGER NOT NULL
);
"""


def resolve_code_db_path(args) -> Path:
    """Same override rule as resolve_db_path, but the filename is always
    "<project>-code.sqlite" -- a completely separate file from
    "<project>.sqlite" (checked by TestCodeIndexIsolation)."""
    if getattr(args, "db", None):
        return Path(args.db).expanduser().resolve()
    base = Path(os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum")))
    return base / f"{args.project}-code.sqlite"


def code_schema_version(conn: sqlite3.Connection) -> int:
    """0 when the code_schema table doesn't exist yet (a brand-new db, or
    one written before schema versioning existed at all)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_schema'"
    ).fetchone():
        return 0
    row = conn.execute("SELECT version FROM code_schema").fetchone()
    return int(row[0]) if row else 0


def _split_sql(script: str) -> list[str]:
    """Split a schema script into individual statements for one-at-a-time
    execution inside a manually managed transaction: conn.executescript()
    issues its own implicit COMMIT before running, so it can never be used
    mid-transaction (the rebuild below needs the drop+recreate+reseed to
    be one atomic unit). Splits on ";\\n" -- the schema never has a
    semicolon inside a string literal -- and drops any piece that is
    blank or comment-only."""
    stmts = []
    for piece in script.split(";\n"):
        piece = piece.strip()
        if not piece or piece.startswith("--"):
            continue
        stmts.append(piece)
    return stmts


def _preserved_code_config(conn: sqlite3.Connection):
    """(project, code_root, langs) rows an older code_meta holds -- the one
    thing a rebuild must carry over, or nothing can reindex automatically.

    `langs` is read from `code_project` when that table exists -- schema
    v2 moved langs OFF code_meta and onto code_project, so a v2 (or later)
    db being rebuilt has no "langs" column on code_meta at all; reading
    only code_meta (as this used to) would silently carry every project
    forward with langs=NULL, wiping the one fact this whole function
    exists to preserve. code_meta's own "langs" column is read only as the
    v1 fallback, for a db old enough to have never had code_project."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_meta'"
    ).fetchone():
        return []
    project_langs = {}
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_project'"
    ).fetchone():
        project_langs = dict(conn.execute("SELECT project, langs FROM code_project"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(code_meta)")}
    meta_langs_expr = "langs" if "langs" in cols else "NULL"
    rows = []
    for project, code_root, meta_langs in conn.execute(
        f"SELECT project, code_root, {meta_langs_expr} FROM code_meta "
        "WHERE code_root IS NOT NULL AND code_root<>''"
    ):
        rows.append((project, code_root, project_langs.get(project, meta_langs)))
    return rows


class CodeIndexTooNew(sqlite3.DatabaseError):
    """Fix-wave item 9: the db's code_schema.version is GREATER than this
    engine's CODE_SCHEMA_VERSION -- written by a newer engine than the one
    running now. Never silently used (an older engine guessing at a newer
    schema's meaning is how data gets corrupted quietly); never silently
    rebuilt either (that would DESTROY a newer engine's index). Derives
    from sqlite3.DatabaseError so every existing `except sqlite3.
    DatabaseError` fail-open path (cmd_code_search, why's code-index fast
    path) already degrades gracefully without new except clauses, but
    callers that want the specific message can catch this type by name."""


def open_code_db(db_path: Path) -> sqlite3.Connection:
    """The code index is a cache with one non-derived fact: which roots and
    languages a project indexes. A db written by an older engine is
    rebuilt empty in one transaction, keeping exactly that fact, so the
    next code-search finds the index stale (not uninitialized) and heals
    it. A db written by a NEWER engine (code_schema.version above this
    engine's CODE_SCHEMA_VERSION) is refused outright -- see
    CodeIndexTooNew."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    version = code_schema_version(conn)
    if version > CODE_SCHEMA_VERSION:
        conn.close()
        raise CodeIndexTooNew(
            f"code index written by a newer engine (schema {version} > {CODE_SCHEMA_VERSION}); "
            "upgrade the engine or delete the db"
        )
    if version < CODE_SCHEMA_VERSION:
        keep = _preserved_code_config(conn)
        try:
            conn.execute("BEGIN")
            for t in CODE_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            for stmt in _split_sql(CODE_SCHEMA_SQL):
                conn.execute(stmt)
            for project, root, langs in keep:
                conn.execute(
                    "INSERT OR IGNORE INTO code_project (project, langs, embedding_mode) VALUES (?,?,'none')",
                    (project, langs),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO code_meta (project, code_root) VALUES (?,?)",
                    (project, root),
                )
            conn.execute("INSERT INTO code_schema (version) VALUES (?)", (CODE_SCHEMA_VERSION,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        conn.executescript(CODE_SCHEMA_SQL)
    return conn


def lang_for_source_file(path: Path) -> str | None:
    """The language of ONE file on disk, or None -- the single resolution
    rule the whole code-index side shares (Anatomy M1 fix wave).

    Extension first (chunkers.lang_for_path, compound-extension aware).
    Only when a file has NO extension at all is its first line sniffed for
    a shebang (chunkers.lang_for_shebang) -- B5: `code-census` already
    counts a `#!/usr/bin/env python3` script named `bin/tool` under the
    python row, so the indexer has to be able to actually index the file
    the census proposed a language on the strength of. Anything else is a
    census that promises what the index silently ignores.

    Guarded to REGULAR files before reading: an extensionless FIFO in the
    tree would otherwise block the read forever, and a directory entry is
    never a source file. Any read failure yields None (fail open) -- an
    unreadable file simply has no resolvable language here; the reindex
    loop's own per-file guard reports it.

    The one resolution rule every code-index caller that needs a file's
    language shares, rather than each answering it separately: the walk's
    classification (`iter_code_source_files`), `cmd_code_reindex`'s
    per-file dispatch (which used to call the extension-only
    `_lang_for_ext`), the per-root staleness check's per-file
    chunker-version comparison (`_root_report`), `code_census`'s
    supported/unsupported classification, and `resolve_symbol_to_path`'s
    disk-scan fallback."""
    lang = chunkers.lang_for_path(path)
    if lang is not None:
        return lang
    if chunkers.extension_of(path):
        return None
    try:
        if not path.is_file() or path.is_symlink():
            return None
        first_line = _first_line_or_none(path)
    except OSError:
        return None
    if not first_line:
        return None
    return chunkers.lang_for_shebang(first_line)


def _root_relative_parts(path: Path, root: Path) -> tuple[str, ...]:
    """`path`'s parts relative to `root`, for the per-file skip test
    (`chunkers.path_is_skipped_for_lang`) both `iter_code_source_files` and
    `code_census` share. Falls back to just the file's own basename if
    `path` doesn't sit under `root` -- defensive only; every path handed in
    here comes from an os.walk rooted at `root`, so this should never
    actually trigger."""
    try:
        return path.relative_to(root).parts
    except ValueError:
        return (path.name,)


def iter_code_source_files(root: Path, langs: list[str] | None, skipped: Counter | None = None):
    """Walk `root`, yielding source files whose language (see
    `lang_for_source_file` -- extension, or a shebang on an extensionless
    file) is in the wired set (`langs`). Dispatch is registry-driven
    rather than the old locally duplicated extension map.

    `langs` defaults to swift-only when falsy -- a legacy-row safety net for
    code_index_report's per-root walk, whose only caller reads it out of an
    existing code_project.langs row that has been non-empty on every row
    ever written since langs became mandatory (Task 7); it does not mean
    code-reindex itself still has a default (it doesn't -- see
    cmd_code_reindex's --lang resolution, which fails outright rather than
    reaching this fallback).

    Directory pruning (fix wave C1, superseding Task 7's union rule): a
    language's skip_dirs prune only THAT language's own files. The walk
    prunes CODE_SKIP_DIR_NAMES (an alias of chunkers.UNIVERSAL_SKIP_DIRS --
    see that set's own docstring for the member list and rationale)
    plus chunkers.common_skip_dirs(wired) -- the INTERSECTION of the wired
    languages' skip sets, a pure optimization since every file under such
    a directory would be dropped by its own language's rule anyway. Every
    other directory is walked, and a file is dropped iff one of its
    root-relative ancestor directory names is in ITS OWN language's skip
    set (chunkers.path_is_skipped_for_lang). Consequence: with swift and
    python both wired, `Tests/foo.py` IS indexed (python's skip set has no
    "Tests") while `Tests/Foo.swift` is not. The union rule dropped both,
    silently losing python source the census had just proposed python on
    the strength of -- see chunkers.common_skip_dirs and
    TestSkipDirOwnLanguageRule.

    `skipped`, when passed a Counter, is mutated in place: every walked file
    that is NOT yielded because its extension is unsupported (no language
    at all) or resolves to a lang outside the wired set (engine-supported
    but not selected for this project) has its extension tallied there --
    keyed compound-extension-aware (chunkers.extension_of, fix wave I2:
    `foo.blade.php` counts as ".blade.php", never ".php") and under
    NO_EXTENSION_BUCKET for an extensionless file with no recognized
    shebang (B5: the same "second silent gap" the census already closed).
    A file dropped by its OWN language's skip set is NOT tallied -- that
    is deliberately-pruned noise, not a blind spot in what this engine can
    chunk. That Counter is the data source for code-reindex's end-of-run
    provenance line (spec S4, INC-0103/0104: no growing blind spot may be
    silent). Mutated-in-place rather than a second yield channel so
    existing callers (_root_report, code_index_report's per-root walk)
    that iterate this generator for plain paths need no change."""
    wired = list(langs or ["swift"])
    wired_set = set(wired)
    skip_dirs = CODE_SKIP_DIR_NAMES | chunkers.common_skip_dirs(wired)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in sorted(filenames):
            path = Path(dirpath) / fname
            lang = lang_for_source_file(path)
            if lang is not None and lang in wired_set:
                rel_parts = _root_relative_parts(path, root)
                if chunkers.path_is_skipped_for_lang(rel_parts, lang):
                    continue
                yield path
                continue
            if skipped is not None:
                skipped[chunkers.extension_of(fname) or NO_EXTENSION_BUCKET] += 1


# ---------------------------------------------------------------------------
# chunker: lexer-aware brace walker -- moved to chunkers/swift.py (Task 2,
# Anatomy M1 milestone). Only `chunk_source` is re-exported here now, for
# the existing tests that import it as memidx.chunk_source. The lexer
# internals (_build_mask_and_match_dict, _KEYWORD_RE, _container_type_name,
# _extract_decls) used to be re-exported too, for declared_symbol_names and
# fragment_declared_in_text's inline Swift container-name pass; both are
# gone (fix wave I3 -- the vocabulary question is now one generic call to
# the backend's own declared_symbols), and with them every reason for this
# module to reach into a backend's internals at all.
# ---------------------------------------------------------------------------

from chunkers.swift import chunk_source


# ---------------------------------------------------------------------------
# embed/FTS text + reindex/search commands
# ---------------------------------------------------------------------------


def split_ident(name: str) -> str:
    """camelCase/snake_case splitter used for both the FTS split_tokens
    column and the embed text -- "writePNG" -> "write PNG",
    "PNGWriter" -> "PNG Writer"."""
    s = name.replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def split_qualified(qualified_name: str) -> str:
    return " ".join(split_ident(part) for part in qualified_name.split(".") if part)


def code_embed_text_for(rel_path: str, chunk: dict, body_lines: list) -> str:
    """file path, qualified name PLUS split tokens, signature, doc comment,
    first ~25 body lines -- in that order (per the spec)."""
    split_tokens = split_qualified(chunk["qualified_name"])
    parts = [
        rel_path,
        f"{chunk['qualified_name']} {split_tokens}".strip(),
        chunk["signature"] or "",
        chunk["doc"] or "",
        "\n".join(body_lines[:25]),
    ]
    return "\n\n".join(p for p in parts if p)


def delete_code_chunks_for_path(conn: sqlite3.Connection, project: str, code_root: str, path: str) -> None:
    rows = conn.execute(
        "SELECT id FROM chunks WHERE project=? AND code_root=? AND path=?", (project, code_root, path)
    ).fetchall()
    for r in rows:
        conn.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
        conn.execute("DELETE FROM embeddings WHERE chunk_id=?", (r["id"],))
    conn.execute("DELETE FROM chunks WHERE project=? AND code_root=? AND path=?", (project, code_root, path))


def drop_code_root(conn: sqlite3.Connection, project: str, code_root: str) -> None:
    """Remove one code root's rows from a project's index -- nothing is
    walked, so this is the correct way to retire a root that is gone for
    good (deleted, moved, unmounted) without needing it to exist on disk."""
    for row in conn.execute(
        "SELECT path FROM file_sha WHERE project=? AND code_root=?", (project, code_root)
    ).fetchall():
        delete_code_chunks_for_path(conn, project, code_root, row["path"])
    conn.execute("DELETE FROM file_sha WHERE project=? AND code_root=?", (project, code_root))
    conn.execute("DELETE FROM code_meta WHERE project=? AND code_root=?", (project, code_root))


def resolve_project_langs(conn: sqlite3.Connection, project: str) -> list[str] | None:
    row = conn.execute("SELECT langs FROM code_project WHERE project=?", (project,)).fetchone()
    if row is None or not row["langs"]:
        return None
    return [l.strip() for l in row["langs"].split(",") if l.strip()]


def _root_is_readable_dir(root: Path) -> bool:
    try:
        return root.is_dir() and os.access(root, os.R_OK | os.X_OK)
    except OSError:
        return False


def _git_head_sha(root: Path) -> str | None:
    """Finding 1 (index provenance): the code repo's HEAD sha at
    code-reindex time, stored in code_meta alongside code_root/
    last_indexed_at, when `root` is (inside) a git repo and git is on
    PATH. Best-effort only -- None (not an exception) on any failure, so a
    non-git code_root or a missing git binary never breaks code-reindex."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


CHUNK_REQUIRED_KEYS = (
    "kind", "symbol", "qualified_name", "signature", "doc",
    "start_line", "end_line", "lang",
)


def validate_chunk_result(result, rel: str) -> None:
    """C3 (Anatomy M1 fix wave, Codex): the reindex loop is the boundary
    between a chunker backend and the database, so it -- not each backend
    -- is where the registry contract is ENFORCED. Raises ValueError
    naming what is wrong; cmd_code_reindex's per-file guard turns that
    into B1's failure path (warn, purge whatever the last good run stored
    for this file, continue), so a violating chunk is never written.

    Checked: a real ChunkResult back (not None, not a bare list); `chunks`
    and `gaps` are lists; every chunk is a dict carrying every key the
    INSERT below reads; `kind` is drawn from the frozen chunkers.KINDS
    vocabulary (spec S2 -- the whole point of freezing it is that nothing
    outside it reaches the column); `start_line`/`end_line` are real ints
    (a string "1" would sort and slice wrongly and poison every downstream
    line-range read). `path` is deliberately NOT required: it is supplied
    by this caller (rel_path), never by the provider."""
    if not isinstance(result, chunkers.ChunkResult):
        raise ValueError(
            f"chunker returned {type(result).__name__}, not a ChunkResult"
        )
    if not isinstance(result.chunks, list) or not isinstance(result.gaps, list):
        raise ValueError("ChunkResult.chunks and .gaps must both be lists")
    if result.status not in ("ok", "partial", "failed"):
        raise ValueError(f"ChunkResult.status {result.status!r} is not ok/partial/failed")
    for gap in result.gaps:
        if not isinstance(gap, (tuple, list)) or len(gap) != 3:
            raise ValueError(f"malformed gap {gap!r} (expected a 3-tuple)")
    for i, chunk in enumerate(result.chunks):
        if not isinstance(chunk, dict):
            raise ValueError(f"chunk {i} is {type(chunk).__name__}, not a dict")
        missing = [k for k in CHUNK_REQUIRED_KEYS if k not in chunk]
        if missing:
            raise ValueError(f"chunk {i} is missing required key(s): {', '.join(missing)}")
        if chunk["kind"] not in chunkers.KINDS:
            raise ValueError(
                f"chunk {i} has kind {chunk['kind']!r}, which is outside the frozen "
                f"kind vocabulary ({', '.join(sorted(chunkers.KINDS))})"
            )
        for key in ("start_line", "end_line"):
            if not isinstance(chunk[key], int) or isinstance(chunk[key], bool):
                raise ValueError(
                    f"chunk {i} has non-integer {key}: {chunk[key]!r}"
                )


# Task 3 (Anatomy M2a): a chunker/contract violation that will keep
# recurring on the same bytes -- fixing it needs a source-code change, not
# a retry -- lands as `failed`. validate_chunk_result raises a bare
# ValueError (no dedicated exception type exists for its contract), and
# the python_ast backend's own SyntaxError-to-status="failed" path is
# turned into the same ValueError by the `result.status == "failed"`
# check below, so both land here uniformly.
DETERMINISTIC_FAILURES = (SyntaxError, ValueError)


def write_file_status(
    conn: sqlite3.Connection, project: str, code_root: str, rel: str, *,
    sha, mtime, size, gap_count, chunker_version, status, reason=None, attempt_key=None,
) -> None:
    """Single writer for every file_sha row cmd_code_reindex produces --
    success (ok/partial) and failure (failed/not-indexed) alike -- so the
    column list lives in exactly one place."""
    conn.execute(
        "INSERT OR REPLACE INTO file_sha (path, project, code_root, sha256, mtime, size, "
        "gap_count, chunker_version, status, reason, attempt_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rel, project, code_root, sha, mtime, size, gap_count, chunker_version, status, reason, attempt_key),
    )


def _refresh_file_sha_stat(conn: sqlite3.Connection, project: str, code_root: str, rel: str, stat) -> None:
    """Single writer for the mtime/size-only refresh (Task 4 carried-in fix
    2, Ruling 57 -- dedupe): content, sha256, gap_count, chunker_version,
    status and reason are all left exactly as stored. Cache bookkeeping
    only -- a bare `touch` (or a not-indexed row left alone, or a report's
    own sha-confirmed match) must not by itself read as a change on the
    next comparison. Used by cmd_code_reindex's unchanged-file and
    left-alone-not-indexed branches and by code_index_report's sha-match
    refresh, so the identical UPDATE lives in exactly one place."""
    conn.execute(
        "UPDATE file_sha SET mtime=?, size=? WHERE project=? AND code_root=? AND path=?",
        (stat.st_mtime, stat.st_size, project, code_root, rel),
    )


def _record_index_failure(conn, project, code_root, rel, f, *, sha, cv, status, reason, attempt_key=None):
    """Shared tail of both failure paths (B1: fail open, purge stale
    state). `f` is best-effort stat'd for mtime/size -- a file that
    vanished mid-run still gets a row, with NULL mtime/size, so the index
    keeps reporting it rather than going silent. (A permission-denied file
    still `stat()`s fine on POSIX -- only the read fails -- so mtime/size
    are usually real for those; NULL is specifically the vanished-file
    case.)"""
    try:
        delete_code_chunks_for_path(conn, project, code_root, rel)
        try:
            st = f.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime = size = None
        write_file_status(
            conn, project, code_root, rel, sha=sha, mtime=mtime, size=size, gap_count=0,
            chunker_version=cv, status=status, reason=reason, attempt_key=attempt_key,
        )
    except Exception:
        pass


def cmd_code_reindex(args) -> int:
    db_path = resolve_code_db_path(args)

    if getattr(args, "drop_root", None):
        try:
            conn = open_code_db(db_path)
        except CodeIndexTooNew as exc:
            print(f"code-reindex: {exc}", file=sys.stderr)
            return 1
        root_s = str(Path(args.drop_root).resolve())
        drop_code_root(conn, args.project, root_s)
        conn.commit()
        conn.close()
        print(f"code-reindex: dropped root {root_s} from the index of {args.project!r}")
        return 0

    if not getattr(args, "code_root", None):
        print("code-reindex: --code-root DIR is required (or --drop-root DIR)", file=sys.stderr)
        return 2

    # Root validation runs BEFORE open_code_db: a refused run must not
    # create a db, and must not trigger the schema v2 rebuild either.
    root = Path(args.code_root).resolve()
    root_s = str(root)
    if not _root_is_readable_dir(root):
        print(
            f"code-reindex: {root_s} is not a readable directory -- nothing was changed; "
            f"if this root is gone for good: code-reindex --drop-root {shlex.quote(root_s)} "
            f"--project {args.project}",
            file=sys.stderr,
        )
        return 2

    # Fix-wave item 9 follow-up (coordinator ruling): a db written by a
    # newer engine must never crash code-reindex either -- same refusal,
    # same message, but code-reindex has real work it could otherwise
    # start (root validation above already ran), so it fails closed with
    # rc=1 rather than code-search/why's rc=0-and-degrade (repo-init turns
    # this 1 into its own exit 13, with the captured stderr).
    try:
        conn = open_code_db(db_path)
    except CodeIndexTooNew as exc:
        print(f"code-reindex: {exc}", file=sys.stderr)
        return 1
    t0 = time.time()

    # Task 7: the old hardcoded "swift" --lang default is gone. Omitted
    # --lang reuses code_project.langs from a PRIOR reindex of this
    # project, when one is stored; a project with no stored langs yet (its
    # first reindex) must name its languages explicitly -- silently
    # defaulting to swift-only used to index nothing at all for a
    # python-only project set up without --lang, and never say why.
    #
    # Anatomy M2a: one project has one language set, shared by every root.
    # A given --lang that is a SUPERSET of the stored set is additive (the
    # shape `--add-lang` produces via repo-init step 7b) and orphans
    # nothing; a set that DROPS a stored language is refused unless
    # --full, which rewrites code_project.langs for every root.
    stored = resolve_project_langs(conn, args.project)
    if getattr(args, "lang", None):
        langs = [l.strip() for l in args.lang.split(",") if l.strip()]

        if stored is not None and not set(stored) <= set(langs) and not args.full:
            removed = ", ".join(sorted(set(stored) - set(langs)))
            print(
                f"code-reindex: --lang {args.lang} drops {removed} from the project's stored "
                f"language set {','.join(stored)}; one project has one language set -- pass a "
                "superset, or --full to change it for every root",
                file=sys.stderr,
            )
            conn.close()
            return 1
    elif stored:
        langs = stored
    else:
        print(
            f"code-reindex: --lang required on first code-reindex for a project "
            f"(no stored langs yet for {args.project!r})",
            file=sys.stderr,
        )
        conn.close()
        return 1

    # C2 (Anatomy M1 fix wave, Codex): every RESOLVED language name --
    # whether it came from --lang or from a code_project row a previous
    # run stored -- must name a real LANGUAGE_TABLE row. A typo used to
    # be tolerated silently: the walk matched zero files for it, the run
    # said nothing, and the bad name was then PERSISTED, so every later
    # run reused it. That is exactly the silent blind spot this milestone
    # exists to close, so it is now a loud failure naming the languages
    # this engine actually knows, before anything is walked or written.
    unknown = [l for l in langs if l not in chunkers.LANGUAGE_TABLE]
    if unknown:
        print(
            f"code-reindex: unknown language(s): {', '.join(unknown)} -- "
            f"this engine version knows: {', '.join(sorted(chunkers.LANGUAGE_TABLE))}",
            file=sys.stderr,
        )
        conn.close()
        return 1

    conn.execute(
        "INSERT INTO code_project (project, langs, embedding_mode) VALUES (?,?,'none') "
        "ON CONFLICT(project) DO UPDATE SET langs=excluded.langs",
        (args.project, ",".join(langs)),
    )

    existing = {
        row["path"]: (row["sha256"], row["chunker_version"], row["status"], row["attempt_key"])
        for row in conn.execute(
            "SELECT path, sha256, chunker_version, status, attempt_key FROM file_sha "
            "WHERE project=? AND code_root=?",
            (args.project, root_s),
        )
    }

    # Anatomy M2a binding point 1: the backend availability fingerprint at
    # THIS run -- computed once (not per file) so a not-indexed row's
    # retry check and the fingerprint it stamps on a fresh not-indexed row
    # agree with each other within one run.
    availability = chunkers.backend_availability()
    retry_not_indexed = getattr(args, "retry_not_indexed", True)

    skipped_unknown: Counter = Counter()
    files = list(iter_code_source_files(root, langs, skipped_unknown))
    seen = set()
    added_files = changed_files = unchanged_files = failed_files = not_indexed_files = 0
    total_gaps = 0
    pending_texts: list = []
    pending_ids: list = []

    # B1/C3 (Anatomy M1 fix wave). Two changes to the per-file loop:
    #
    # (a) the try covers the WHOLE per-file body -- read_bytes/stat/decode
    #     and the chunk INSERT loop, not just the chunk_file call. Grok
    #     HIGH 2: one mode-000 (or vanished, or malformed-result) file used
    #     to abort the entire walk with a traceback, so a single
    #     unreadable file could leave most of a repo unindexed.
    #
    # (b) a failure PURGES this file's stale index state instead of merely
    #     declining to write new state (Grok HIGH 1). Writing no file_sha
    #     row was enough to make repair retrigger, but the chunks the LAST
    #     good run stored stayed in the table -- so code-search kept
    #     answering from rows the current source text no longer produces,
    #     with nothing on any surface saying so. Now: delete the chunks
    #     (with their fts/embedding shadows) AND the file_sha row, warn
    #     naming the file, continue. Deleting the file_sha row is also what
    #     keeps code_index_report honest: the file is on disk, absent from
    #     file_sha, so the index reports "stale", not "current".
    #
    # A "partial" status still indexes its chunks and writes file_sha with
    # gap_count = len(gaps) -- the existing gap behavior, unchanged.
    for f in files:
        try:
            rel = str(f.relative_to(root))
        except ValueError:
            rel = str(f)
        # `seen` is populated BEFORE the guarded body: a file that failed
        # to chunk is still a file that EXISTS on disk, so it must not
        # also be swept up by the removed-paths pass below and counted as
        # a deletion.
        seen.add(rel)
        sha = cv = None
        try:
            # Task 4 carried-in fix 1 (Task 3 review, Ruling 57): lang, cv
            # and the not-indexed retry decision need only the path and the
            # table -- never the file's bytes -- so they run BEFORE
            # f.read_bytes(). Previously read_bytes() ran first, so an
            # unreadable (or vanished-mid-run) file never reached the
            # leave-it-alone skip below and was re-attempted -- and
            # re-counted as not-indexed -- on every run, inflating the
            # heal's re-indexed sum.
            lang = lang_for_source_file(f)
            try:
                cv = chunkers.chunker_version(lang)
            except KeyError:
                # Task 3 edge case: a lang with no LANGUAGE_TABLE row must
                # not crash code-reindex -- fail open with a literal stamp
                # instead of raising. (Unreachable via --lang since C2
                # validates the resolved language set; kept as a guard.)
                cv = "unversioned"
            prev = existing.get(rel)
            if prev is not None and prev[2] == "not-indexed":
                # Task 3 / binding point 1: a not-indexed row is retried
                # only when this run is explicit (retry_not_indexed,
                # default True; the heal passes False), --full, or the
                # backend/chunker moved on since the row was stamped --
                # otherwise it is left exactly as-is (still not-indexed)
                # and counted as unchanged, so an unrelated edit elsewhere
                # in the tree never retries a known-broken backend on
                # every heal.
                prev_sha, prev_cv, _prev_status, prev_attempt_key = prev
                retry = (
                    retry_not_indexed
                    or args.full
                    or prev_attempt_key != availability
                    or prev_cv != cv
                )
                if not retry:
                    unchanged_files += 1
                    try:
                        _refresh_file_sha_stat(conn, args.project, root_s, rel, f.stat())
                    except OSError:
                        pass  # best-effort only -- the row itself stays exactly as stamped
                    continue
                prev_sha = prev_cv = None  # a not-indexed row never carries a real sha to compare
            else:
                prev_sha, prev_cv = (prev[0], prev[1]) if prev is not None else (None, None)

            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            if prev_sha == sha and prev_cv == cv and not args.full:
                unchanged_files += 1
                # Finding 2 (staleness): the file's content (and thus its
                # chunks) didn't change, but its mtime/size on disk may
                # have (e.g. a bare `touch`) -- refresh the stored
                # file_sha row's mtime/size so code_index_report's
                # on-disk comparison matches again. sha256/gap_count/
                # chunker_version are untouched (nothing about the indexed
                # content or the chunker that produced it changed), so
                # this is a plain UPDATE, not the INSERT OR REPLACE the
                # changed/added branch below uses.
                _refresh_file_sha_stat(conn, args.project, root_s, rel, f.stat())
                if not args.no_embed:
                    # embedding_mode's invariant is REAL coverage (every
                    # chunk of a `full` project has a vector), not merely
                    # "the last run wasn't --no-embed" -- an unchanged
                    # file's chunks may still lack embeddings left by an
                    # earlier --no-embed run, so top those up without
                    # re-chunking a file whose content didn't move.
                    missing = conn.execute(
                        "SELECT c.id, c.qualified_name, c.signature, c.doc, c.start_line, c.end_line "
                        "FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.id "
                        "WHERE c.project=? AND c.code_root=? AND c.path=? AND e.chunk_id IS NULL",
                        (args.project, root_s, rel),
                    ).fetchall()
                    if missing:
                        unchanged_text_lines = data.decode("utf-8", errors="replace").splitlines()
                        for mrow in missing:
                            body_lines = unchanged_text_lines[mrow["start_line"] : mrow["end_line"] - 1]
                            stub = {
                                "qualified_name": mrow["qualified_name"],
                                "signature": mrow["signature"],
                                "doc": mrow["doc"],
                            }
                            pending_texts.append(code_embed_text_for(rel, stub, body_lines))
                            pending_ids.append(mrow["id"])
                continue
            is_new = rel not in existing

            text = data.decode("utf-8", errors="replace")
            # Task 5 rewire: dispatch through the chunker registry instead
            # of calling the Swift walker (chunk_source) directly --
            # get_chunker(lang) resolves the right backend, chunk_file(text,
            # rel) is the uniform per-backend contract (ChunkResult:
            # chunks/gaps/status), and validate_chunk_result enforces that
            # contract here at the DB boundary (C3).
            result = chunkers.get_chunker(lang).chunk_file(text, rel)
            validate_chunk_result(result, rel)

            if result.status == "failed":
                raise ValueError("chunker reported status=failed")

            for g in result.gaps:
                total_gaps += 1
                print(
                    f"code-reindex: WARNING gap in {rel} lines {g[0]}-{g[1]} "
                    f"({g[2]}, skipped)",
                    file=sys.stderr,
                )

            chunks = result.chunks
            gaps = result.gaps

            delete_code_chunks_for_path(conn, args.project, root_s, rel)
            text_lines = text.splitlines()
            # Embeddings are queued per-file and only merged into the
            # shared pending lists once the whole file is stored: a chunk
            # id whose row is deleted again by the failure path below must
            # never reach the embeddings table.
            file_texts: list = []
            file_ids: list = []
            for chunk in chunks:
                body_lines = text_lines[chunk["start_line"] : chunk["end_line"] - 1]
                cur = conn.execute(
                    """INSERT INTO chunks (path, project, code_root, lang, kind, symbol, qualified_name,
                           signature, doc, start_line, end_line)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rel, args.project, root_s, chunk["lang"], chunk["kind"], chunk["symbol"],
                        chunk["qualified_name"], chunk["signature"], chunk["doc"],
                        chunk["start_line"], chunk["end_line"],
                    ),
                )
                chunk_id = cur.lastrowid
                split_tokens = split_qualified(chunk["qualified_name"])
                body_text = "\n".join(body_lines[:25])
                conn.execute(
                    "INSERT INTO fts (rowid, qualified_name, split_tokens, signature, doc, body) "
                    "VALUES (?,?,?,?,?,?)",
                    (chunk_id, chunk["qualified_name"], split_tokens, chunk["signature"] or "",
                     chunk["doc"] or "", body_text),
                )
                if not args.no_embed:
                    file_texts.append(code_embed_text_for(rel, chunk, body_lines))
                    file_ids.append(chunk_id)

            stat = f.stat()
            write_file_status(
                conn, args.project, root_s, rel, sha=sha, mtime=stat.st_mtime, size=stat.st_size,
                gap_count=len(gaps), chunker_version=cv, status="partial" if gaps else "ok",
            )
            pending_texts.extend(file_texts)
            pending_ids.extend(file_ids)
            if is_new:
                added_files += 1
            else:
                changed_files += 1
        except DETERMINISTIC_FAILURES as exc:
            # Task 3: a chunker/contract violation that will keep
            # recurring on the same bytes -- fixing it needs a source
            # change, not a retry. B1's purge-then-warn shape, but the
            # file_sha row STAYS (status=failed, sha+chunker_version
            # stored) so it is skipped incrementally while the source and
            # chunker are unchanged, and retried on a source edit or
            # --full -- never silently, and never every run.
            _record_index_failure(
                conn, args.project, root_s, rel, f, sha=sha, cv=cv, status="failed",
                reason=f"{type(exc).__name__}: {exc}",
            )
            failed_files += 1
            print(
                f"code-reindex: WARNING {rel} failed to index: {type(exc).__name__}: {exc} -- "
                "previous chunks removed; retried when the file or the chunker changes, or with --full",
                file=sys.stderr,
            )
            continue
        except Exception as exc:
            # B1: purge whatever this path still has in the index, so no
            # stale row outlives the source that produced it. Task 3: the
            # row itself STAYS (status=not-indexed, sha NULL, chunker_version
            # from the table, attempt_key = this run's backend availability
            # fingerprint) rather than being deleted -- this is the
            # retryable bucket (a missing backend, a permission error, any
            # other exception this engine did not itself validate), and the
            # index still reads "stale" while it holds a not-indexed row.
            _record_index_failure(
                conn, args.project, root_s, rel, f, sha=None, cv=cv, status="not-indexed",
                reason=f"{type(exc).__name__}: {exc}", attempt_key=availability,
            )
            not_indexed_files += 1
            print(
                f"code-reindex: {rel} not indexed: {type(exc).__name__}: {exc} "
                "(retried on the next run)",
                file=sys.stderr,
            )
            continue

    reembeds = 0
    if pending_texts:
        vecs = compute_embeddings(pending_texts)
        for cid, v in zip(pending_ids, vecs):
            packed = pack_vector(v)
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (chunk_id, project, dim, vector) VALUES (?,?,?,?)",
                (cid, args.project, len(unpack_vector(packed)), packed),
            )
        reembeds = len(pending_texts)

    removed = set(existing.keys()) - seen
    for rel in removed:
        delete_code_chunks_for_path(conn, args.project, root_s, rel)
        conn.execute(
            "DELETE FROM file_sha WHERE project=? AND code_root=? AND path=?", (args.project, root_s, rel)
        )

    # Task 7: the "swift" fallback here is gone -- `langs` is always a
    # non-empty, resolved list by this point (explicit --lang, or reused
    # code_project.langs; the no-langs-yet case already returned 1 above),
    # so a fallback here would just be dead code hiding a real bug if one
    # of those guarantees ever broke.
    conn.execute(
        "INSERT OR REPLACE INTO code_meta (project, code_root, last_indexed_at, head_sha) "
        "VALUES (?,?,?,?)",
        (args.project, root_s, time.time(), _git_head_sha(root)),
    )

    # Anatomy M2a binding point 2: embedding_mode never lies. A --no-embed
    # run that adds or changes chunks on a `full` project downgrades the
    # project to `none` in the SAME transaction (the invariant tested is
    # actual coverage: every chunk of a `full` project has an embedding
    # row); a --no-embed run that changes nothing leaves whatever mode was
    # stored intact.
    #
    # Fix-wave item 1: a run WITHOUT --no-embed must not set `full` just
    # because it embedded everything it walked -- it only ever sees the
    # root(s) it was given, and another root's chunks may still lack
    # vectors (from an earlier --no-embed run there, or one never run at
    # all). So after the embedding writes above, derive the mode from
    # actual PROJECT-wide coverage: `full` iff no chunk of the project (any
    # root) lacks an embeddings row.
    mutated = (added_files + changed_files) > 0
    mode_now = conn.execute(
        "SELECT embedding_mode FROM code_project WHERE project=?", (args.project,)
    ).fetchone()["embedding_mode"]
    downgraded = False
    stranded_elsewhere = 0
    if args.no_embed:
        if mutated and mode_now == "full":
            conn.execute(
                "UPDATE code_project SET embedding_mode='none' WHERE project=?", (args.project,)
            )
            downgraded = True
    else:
        stranded_elsewhere = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN embeddings e ON e.chunk_id = c.id "
            "WHERE c.project=? AND e.chunk_id IS NULL",
            (args.project,),
        ).fetchone()["n"]
        conn.execute(
            "UPDATE code_project SET embedding_mode=? WHERE project=?",
            ("none" if stranded_elsewhere else "full", args.project),
        )

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    print(
        f"code-reindex: {len(files)} files scanned, {added_files} added, {changed_files} changed, "
        f"{unchanged_files} unchanged, {len(removed)} removed, {failed_files} failed, "
        f"{not_indexed_files} not indexed, {reembeds} chunk(s) (re-)embedded, {total_gaps} gap(s) warned, "
        f"{elapsed:.3f}s"
    )
    if downgraded:
        print(
            f"code-reindex: embeddings are now incomplete for {args.project}; embedding mode set "
            "to none (run without --no-embed to restore)"
        )
    if stranded_elsewhere:
        print(
            f"code-reindex: {stranded_elsewhere} chunk(s) in other roots have no embedding; "
            "embedding mode stays none (run code-reindex without --no-embed on every root)"
        )
    if skipped_unknown:
        # Task 5 census (spec S4, INC-0103/0104 lesson): a walked file whose
        # extension isn't in the wired lang set is NOT indexed -- say so,
        # every run, so the gap never grows silently. One line, extensions
        # sorted by count desc (ties broken alphabetically for determinism).
        total_skipped = sum(skipped_unknown.values())
        breakdown = " ".join(
            f"{ext}={count}"
            for ext, count in sorted(skipped_unknown.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        print(
            f"code-reindex: {total_skipped} files with unsupported/unwired extensions "
            f"not indexed: {breakdown}"
        )
    return 0


# ---------------------------------------------------------------------------
# code-census (Task 8, Anatomy M1): discovery-only, no DB, no consent
# recorded -- census PROPOSES a language set (spec §3,
# docs/internal/DESIGN-anatomy-chunkers.md); repo-init's dialogue (Task 10)
# is what may eventually wire/install/record one. Exit 0 always: a census
# never fails a scan it can walk.
# ---------------------------------------------------------------------------

NO_EXTENSION_BUCKET = "(no extension)"


def _first_line_or_none(path: Path) -> str | None:
    """Read just the first line of `path`, fail-open. Any error (permission
    denied, undecodable bytes, empty file) yields None rather than raising
    -- census never fails a scan it can walk (brief's exit-0-always
    contract). `errors="replace"` means an undecodable byte never raises
    either; it just can never match a shebang afterward, same as None.
    `readline(512)` caps the read on an extensionless file with no
    newline near the start (e.g. a large binary) -- a shebang interpreter
    name is always well within the first few dozen bytes, so a truncated
    line still matches correctly; this only bounds how much of a
    non-matching file gets pulled into memory."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readline(512).rstrip("\n") or None
    except OSError:
        return None


def code_census(root: Path) -> dict:
    """Walk `root` and classify every file two ways: SUPPORTED (resolves to
    a LANGUAGE_TABLE lang via `lang_for_source_file` -- extension first,
    else a shebang sniff for a REGULAR, non-symlinked extensionless file,
    the one resolution rule the whole code-index side shares) or
    UNSUPPORTED (an extension with no LANGUAGE_TABLE row, or an
    extensionless file -- or a symlink, or any other non-regular entry --
    whose language does not resolve, bucketed under NO_EXTENSION_BUCKET
    when it also has no extension). This is the "three ways" the brief
    names: extension-supported, extension-unsupported, and shebang-sniffed
    extensionless (itself supported or unsupported depending on whether the
    shebang matched) -- the shebang path folds into the SAME lang key an
    extension match would use, not a separate status, so a
    `#!/usr/bin/env python3` script and a `foo.py` file both count under
    the "python" key. An extensionless symlink is never followed for a
    shebang sniff (`lang_for_source_file`'s own guard) -- it always lands
    in NO_EXTENSION_BUCKET as unsupported, the same as any other
    non-regular file, so the census never promises a language for a file
    the indexer's own walk would not read either.

    Returns {key: {"files": n, "status": "supported"|"unsupported"}} (the
    brief's JSON shape) -- `key` is a lang name for a supported row, else
    the raw extension string (or NO_EXTENSION_BUCKET) for an unsupported
    one.

    Directory pruning: the walk prunes CODE_SKIP_DIR_NAMES (an alias of
    chunkers.UNIVERSAL_SKIP_DIRS -- see that set's own docstring for the
    member list and rationale) -- the same universal noise set
    iter_code_source_files prunes, and nothing wider. A SUPPORTED file is
    then dropped only when an ancestor directory on its root-relative path
    sits in ITS OWN language's skip_dirs (chunkers.path_is_skipped_for_lang
    against `_root_relative_parts`, the same per-file test and path helper
    iter_code_source_files already applies once a language is wired). An
    UNSUPPORTED extension has no language and so no skip_dirs of its own
    -- it is always counted outside the universal noise dirs, never hidden
    behind another language's skip set. Fails open per file and never
    raises on a walk it can complete: os.walk over a missing/unreadable
    root just yields nothing, so an empty or nonexistent root returns the
    zero-seeded rows below -- every LANGUAGE_TABLE language at 0, no
    unsupported rows at all -- rather than an error (or an empty dict)."""
    skip_dirs = CODE_SKIP_DIR_NAMES
    # C5 (Anatomy M1 fix wave, Codex): EVERY LANGUAGE_TABLE language gets a
    # row, seeded at zero, whether or not the tree holds one of its files.
    # The driven install flow (skills/memcontinuum/SKILL.md step 2) has to
    # present "supported but not found" alongside "proposed", and a
    # language simply missing from the JSON forces every consumer to
    # re-derive the known-language list for itself to spot the difference.
    counts: dict = {
        lang: {"files": 0, "status": "supported"} for lang in chunkers.LANGUAGE_TABLE
    }

    def bump(key: str, status: str) -> None:
        row = counts.setdefault(key, {"files": 0, "status": status})
        row["files"] += 1

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in sorted(filenames):
            path = Path(dirpath) / fname
            # lang_for_source_file is the one resolution rule the whole
            # code-index side shares: extension first (I2 compound-aware,
            # so `foo.blade.php` is never folded into `.php`), else a
            # shebang sniff for an extensionless file (controller-scope
            # addition, Task 5 reviewer finding -- the "second silent
            # gap": extensionless scripts used to vanish from the census
            # entirely).
            lang = lang_for_source_file(path)
            if lang is None:
                ext = chunkers.extension_of(fname)
                bump(ext or NO_EXTENSION_BUCKET, "unsupported")
                continue
            rel_parts = _root_relative_parts(path, root)
            if chunkers.path_is_skipped_for_lang(rel_parts, lang):
                continue
            bump(lang, "supported")

    return counts


def _print_code_census_table(counts: dict) -> None:
    """Human-readable form: supported rows FIRST (named by lang), then
    unsupported rows (named by extension / NO_EXTENSION_BUCKET), each
    group sorted by file count descending, ties broken alphabetically for
    determinism (same tie rule as code-reindex's skipped-extension
    provenance line)."""
    def sort_key(item):
        key, row = item
        return (-row["files"], key)

    supported = sorted(
        (item for item in counts.items() if item[1]["status"] == "supported"),
        key=sort_key,
    )
    unsupported = sorted(
        (item for item in counts.items() if item[1]["status"] == "unsupported"),
        key=sort_key,
    )
    total = sum(row["files"] for row in counts.values())
    print(f"code-census: {total} file(s) scanned")
    if supported:
        # C5: a language the tree does not hold is listed here at 0
        # ("supported but not found"), not omitted -- same data the --json
        # form now carries, in the human's form.
        print("supported:")
        for key, row in supported:
            print(f"  {key}: {row['files']}")
    if unsupported:
        print("unsupported:")
        for key, row in unsupported:
            print(f"  {key}: {row['files']}")
    if total == 0:
        print("(no files found)")


def cmd_code_census(args) -> int:
    """`code-census --root DIR [--json]`. No DB, no --project, no consent
    recorded -- pure discovery (see the module comment above). Exit 0
    always."""
    root = Path(args.root)
    counts = code_census(root)
    if getattr(args, "json", False):
        print(json.dumps(counts))
    else:
        _print_code_census_table(counts)
    return 0


def code_hits_fts(conn: sqlite3.Connection, query: str, project: str, limit: int = 200):
    """Task 7 (Anatomy M2a): a bare identifier-shaped query (e.g. a symbol
    or qualified name typed verbatim, not a phrase) puts every chunk whose
    OWN `symbol` or `qualified_name` equals it first, ordered by path --
    ahead of the raw bm25 ranking, which weighs a short chunk repeating
    the name in its body/doc as favorably as the chunk the name actually
    names (Codex's `parse_frontmatter`-vs-a-test-fixture probe). A
    multi-word or punctuation-bearing query is unaffected -- bm25 ordering
    only, exactly as before."""
    exact: list = []
    q_stripped = query.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", q_stripped):
        exact = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM chunks WHERE project=? AND (symbol=? OR qualified_name=?) "
                "ORDER BY path, start_line",
                (project, q_stripped, q_stripped),
            )
        ]

    q = fts_escape(query)
    rows = conn.execute(
        """SELECT fts.rowid AS rowid FROM fts
           JOIN chunks ON chunks.id = fts.rowid
           WHERE fts MATCH ? AND chunks.project=?
           ORDER BY bm25(fts) LIMIT ?""",
        (q, project, limit),
    ).fetchall()
    ids = [r["rowid"] for r in rows]
    exact_set = set(exact)
    return exact + [i for i in ids if i not in exact_set]


def code_hits_vector(conn: sqlite3.Connection, query: str, project: str):
    qvec = compute_query_embedding(query)
    rows = conn.execute(
        "SELECT chunk_id, vector FROM embeddings WHERE project=?", (project,)
    ).fetchall()
    scored = [(r["chunk_id"], cosine(qvec, unpack_vector(r["vector"]))) for r in rows]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def _root_report(conn: sqlite3.Connection, project: str, root_s: str, langs: list[str] | None, avail: str) -> dict:
    """One code_root's slice of `code_index_report`: preflight only, never
    a mutation of index content -- the tree and the stored `file_sha` rows
    for THIS root are compared, and a drifted-but-sha-confirmed row's
    mtime/size is refreshed (cache bookkeeping, committed by the caller).
    No chunk, status, reason, or meta row is ever written here.

    `avail` is the backend availability fingerprint for this WHOLE report
    call (chunkers.backend_availability() itself warns against recomputing
    it per call within one run) -- every root in one code_index_report
    call is judged against the same fingerprint.

    `changed` counts: a row missing entirely, OR a drifted row (mtime/size
    disagree) whose recomputed sha256 differs from the stored one (binding
    point 4: a drifted row with sha NULL -- a not-indexed file -- carries
    no source evidence and is NEVER counted here; it is governed by
    cmd_code_reindex's retry rule alone), OR a stored `chunker_version`
    that no longer matches what this engine would produce today (every
    status, not-indexed included -- the table stamps its version even when
    the chunker itself could not run), plus every path on disk with no
    surviving row (`removed`)."""
    root = Path(root_s)
    rep = {
        "code_root": root_s, "exists": _root_is_readable_dir(root), "changed": 0, "removed": 0,
        "failed": 0, "not_indexed": 0,
    }
    rows = {
        r["path"]: r
        for r in conn.execute(
            "SELECT path, sha256, mtime, size, chunker_version, status, attempt_key FROM file_sha "
            "WHERE project=? AND code_root=?",
            (project, root_s),
        )
    }
    rep["failed"] = sum(1 for r in rows.values() if r["status"] == "failed")
    rep["not_indexed"] = sum(1 for r in rows.values() if r["status"] == "not-indexed")
    rep["availability_changed"] = any(
        r["status"] == "not-indexed" and r["attempt_key"] != avail for r in rows.values()
    )
    if not rep["exists"]:
        # A missing root contributes exists: False and does not itself
        # count as changed -- code_index_report folds this into `degraded`
        # (never `current`) via missing_root, binding point 5.
        return rep
    seen = set()
    touched = False
    for f in iter_code_source_files(root, langs):
        try:
            rel = str(f.relative_to(root))
        except ValueError:
            continue
        seen.add(rel)
        prev = rows.get(rel)
        if prev is None:
            rep["changed"] += 1
            continue
        try:
            cv = chunkers.chunker_version(lang_for_source_file(f))
        except KeyError:
            cv = "unversioned"
        if prev["chunker_version"] != cv:
            rep["changed"] += 1
            continue
        if prev["sha256"] is None:
            continue  # not-indexed: no source evidence -- the retry rule owns it
        try:
            st = f.stat()
        except OSError:
            rep["changed"] += 1
            continue
        if prev["mtime"] != st.st_mtime or prev["size"] != st.st_size:
            try:
                if hashlib.sha256(f.read_bytes()).hexdigest() != prev["sha256"]:
                    rep["changed"] += 1
                else:
                    _refresh_file_sha_stat(conn, project, root_s, rel, st)
                    touched = True
            except OSError:
                rep["changed"] += 1
    rep["removed"] = len(set(rows) - seen)
    rep["changed"] += rep["removed"]
    if touched:
        conn.commit()
    return rep


def code_index_report(conn: sqlite3.Connection, project: str) -> dict:
    """Finding 1 (HIGH, index provenance) + Anatomy M2a Task 4: the
    preflight code-search consults before every search and code-reindex's
    heal (Task 5) consumes to decide what to repair -- per code_root,
    status-aware, and sha-confirmed (a drifted-but-unchanged file is never
    reported as a change). Read-only except for the mtime/size cache
    refresh `_root_report` may commit; never writes a chunk, status, or
    meta row.

    State, in order: no roots at all -> "uninitialized" (code-reindex was
    never run for this project); any root's `changed > 0` -> "stale";
    else `not_indexed > 0`, or `availability_changed`, or any root missing
    on disk -> "degraded" (binding point 5: a missing recorded root can
    never read "current"); else "current"."""
    proj = conn.execute(
        "SELECT langs, embedding_mode FROM code_project WHERE project=?", (project,)
    ).fetchone()
    metas = conn.execute(
        "SELECT code_root, last_indexed_at, head_sha FROM code_meta WHERE project=? ORDER BY code_root",
        (project,),
    ).fetchall()
    if not metas:
        return {
            "state": "uninitialized", "langs": None, "embedding_mode": "none",
            "availability_changed": False, "roots": [], "changed": 0, "failed": 0, "not_indexed": 0,
        }
    langs = resolve_project_langs(conn, project)
    avail = chunkers.backend_availability()  # one fingerprint for every root in this call
    roots = []
    for m in metas:
        r = _root_report(conn, project, m["code_root"], langs, avail)
        r["last_indexed_at"] = m["last_indexed_at"]
        r["head_sha"] = m["head_sha"]
        roots.append(r)
    changed = sum(r["changed"] for r in roots)
    failed = sum(r["failed"] for r in roots)
    not_indexed = sum(r["not_indexed"] for r in roots)
    # Fix-wave item 4 (Grok G2): a MISSING root's own availability_changed
    # flag must never make the project eligible for heal -- heal_code_index
    # skips exists=False roots outright (there is nothing there to
    # re-index), so a missing root's stale not-indexed rows can never
    # actually get resolved by a heal attempt; counting them into the
    # aggregate just re-triggers "eligible" on every single search forever,
    # for a heal that touches nothing. The per-root flag itself still
    # reflects reality (_root_report computes it before checking `exists`);
    # only the project-wide aggregate ignores a root that isn't there. A
    # missing root still forces `degraded` via missing_root below.
    availability_changed = any(r["availability_changed"] for r in roots if r["exists"])
    missing_root = any(not r["exists"] for r in roots)
    if changed:
        state = "stale"
    elif not_indexed or availability_changed or missing_root:
        state = "degraded"
    else:
        state = "current"
    return {
        "state": state, "langs": langs, "embedding_mode": (proj["embedding_mode"] if proj else "none"),
        "availability_changed": availability_changed, "roots": roots, "changed": changed,
        "failed": failed, "not_indexed": not_indexed,
    }


def heal_code_index(conn: sqlite3.Connection, db_path: Path, project: str, report: dict, *, limit: int):
    """Anatomy M2a Task 5: `code-search`'s one-attempt preflighted heal --
    consulted right after code_index_report, before a search ever answers.
    Eligible when the report is `stale` or `degraded` AND has something to
    actually fix (`changed > 0` or `availability_changed` -- a `failed`-only
    report is left alone; `code-reindex` is what a real parse failure
    needs, not an automatic retry loop). Within --heal-limit, every
    recorded root that still exists on disk is reindexed ONCE, in-process,
    with `retry_not_indexed=False` (binding point 1: an unrelated edit must
    never retry a known-broken backend) and `--full` never set (a heal
    repairs drift, it does not rewrite the project's language set).
    `no_embed` follows the project's OWN embedding_mode (binding point 2):
    `full` embeds, anything else passes --no-embed, so a heal can never be
    the thing that silently adds unembedded chunks to a `full` project.

    Returns (report, conn) -- conn is always a LIVE, valid connection on
    return, never the one this function may have closed along the way;
    cmd_code_search's caller reads report/conn from this call's own return
    value, never the ones it passed in.

    Any exception during the heal is fail-open: the db is reopened, the
    ORIGINAL (pre-heal) report is returned unchanged, and code-search still
    answers from whatever was already indexed -- a heal that cannot finish
    must never crash a search or leave the connection closed."""
    eligible = report["state"] in ("stale", "degraded") and (report["changed"] > 0 or report["availability_changed"])
    if not eligible:
        return report, conn
    if report["changed"] > limit:
        # Fix-wave item 7: name the report's actual state, not a hardcoded
        # "stale" -- this refusal fires for a `degraded` report too (heal
        # is eligible on `stale` OR `degraded`), and "index is stale" is
        # simply wrong when the report says degraded.
        print(
            f"code-search: index is {report['state']} ({report['changed']} file(s) changed since "
            f"the last code-reindex, above --heal-limit {limit}); run code-reindex",
            file=sys.stderr,
        )
        return report, conn
    langs = ",".join(report["langs"] or [])
    no_embed = report["embedding_mode"] != "full"
    # Task 5 decision: matches the actual per-run summary line printed at
    # the end of cmd_code_reindex (see the `code-reindex: {N} files
    # scanned, ... added, ... changed, ... unchanged, ... removed, ...
    # failed, ... not indexed, ...` line) -- the "healed" count sums every
    # file this heal actually TOUCHED (added + changed + failed + not
    # indexed), never the preflight's own `changed` count (binding point 4).
    summary_re = re.compile(r"(\d+) added, (\d+) changed, .*? (\d+) failed, (\d+) not indexed")
    try:
        conn.close()
        reindexed = 0
        for r in report["roots"]:
            if not r["exists"]:
                continue
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                cmd_code_reindex(argparse.Namespace(
                    code_root=r["code_root"], drop_root=None, db=str(db_path), project=project,
                    lang=langs or None, no_embed=no_embed, full=False, retry_not_indexed=False,
                ))
            m = summary_re.search(out.getvalue())
            if m:
                reindexed += sum(int(g) for g in m.groups())
        conn = open_code_db(db_path)
        after = code_index_report(conn, project)
        if after["state"] == "current":
            print(f"code-search: index healed ({reindexed} file(s) re-indexed)", file=sys.stderr)
        return after, conn
    except Exception as exc:
        # Fix-wave item 6: `conn` may already be the REOPENED connection
        # (code_index_report itself raised, after the line above swapped
        # `conn` back to a live handle) -- close it before reassigning, or
        # that connection leaks. try/except because `conn` may equally
        # still be the one this function already closed a few lines up
        # (the reindex loop itself raised, before the reopen ran); closing
        # an already-closed sqlite3.Connection is harmless, but nothing
        # here should depend on that.
        try:
            conn.close()
        except Exception:
            pass
        conn = open_code_db(db_path)
        print(
            f"code-search: heal failed ({type(exc).__name__}: {exc}); answering from the current index",
            file=sys.stderr,
        )
        return report, conn


def cmd_code_search(args) -> int:
    db_path = resolve_code_db_path(args)
    # Fix-wave item 9: a db written by a newer engine must never crash
    # code-search -- fail open with the refusal's own message and no
    # results, same shape as the uninitialized-project case below.
    try:
        conn = open_code_db(db_path)
    except CodeIndexTooNew as exc:
        print(f"code-search: {exc}", file=sys.stderr)
        if args.json:
            print(json.dumps(
                {"state": "unavailable", "code_root": None, "indexed_at": None,
                 "head_sha": None, "results": []},
                indent=2,
            ))
        return 0

    report = code_index_report(conn, args.project)

    if report["state"] == "uninitialized":
        print(
            f"code-search: the code index is uninitialized for project {args.project!r} "
            f"(no code_meta / no chunks for this root) -- run `code-reindex` first",
            file=sys.stderr,
        )
        conn.close()
        if args.json:
            print(json.dumps(
                {"state": "uninitialized", "code_root": None, "indexed_at": None,
                 "head_sha": None, "results": []},
                indent=2,
            ))
        return 1

    # Anatomy M2a Task 5: one preflighted heal attempt before anything else
    # reads `report` -- every message below (and the --json envelope) is
    # computed from whatever heal_code_index returns, which may still be
    # stale/degraded (heal_limit refused, nothing eligible, or a heal that
    # itself failed open) or may now read current.
    if not getattr(args, "no_heal", False):
        report, conn = heal_code_index(
            conn, db_path, args.project, report, limit=getattr(args, "heal_limit", 500)
        )
    state = report["state"]

    # Anatomy M2a Task 4: the preflight now distinguishes stale / degraded /
    # failed / a missing root and says each one that applies, instead of a
    # single "appears stale" catch-all. A missing root is named regardless
    # of overall state (binding point 5 keeps it out of "current", but the
    # line itself is the actionable one -- `code-reindex --drop-root` if
    # it is gone for good).
    if state == "stale":
        print(
            f"code-search: WARNING code index is stale ({report['changed']} file(s) differ "
            "from the last code-reindex)",
            file=sys.stderr,
        )
    elif state == "degraded":
        print(
            f"code-search: code index is incomplete ({report['not_indexed']} file(s) not indexed)",
            file=sys.stderr,
        )
    if report["failed"] > 0:
        print(
            f"code-search: {report['failed']} file(s) failed to index -- see code-reindex output",
            file=sys.stderr,
        )
    for r in report["roots"]:
        if not r["exists"]:
            print(
                f"code-search: WARNING recorded code root {r['code_root']} does not exist -- "
                f"code-reindex --drop-root {shlex.quote(r['code_root'])} if it is gone for good",
                file=sys.stderr,
            )

    if args.mode == "fts":
        ids = code_hits_fts(conn, args.query, args.project)
        results = [(cid, float(len(ids) - i)) for i, cid in enumerate(ids)]
    elif args.mode == "vector":
        results = code_hits_vector(conn, args.query, args.project)
    elif args.mode == "hybrid":
        fts_ids = code_hits_fts(conn, args.query, args.project)
        vec_ids = [cid for cid, _ in code_hits_vector(conn, args.query, args.project)]
        k = 60
        scores: dict = {}
        for i, cid in enumerate(fts_ids):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
        for i, cid in enumerate(vec_ids):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
        results = sorted(scores.items(), key=lambda t: t[1], reverse=True)
    else:
        raise ValueError(f"unknown mode {args.mode}")

    results = results[: args.limit]

    # Finding 3: concept attachment must always read the DECISION
    # (markdown) db, never the CODE db that --db selects for this command.
    # resolve_db_path(args) would incorrectly honor a --db that here means
    # "the code db" (schema clash: SCHEMA_SQL run onto the code db, and
    # concepts read from a db that has none -- attach silently empty). A
    # dedicated --decision-db flag lets a caller override the decision db
    # explicitly; absent that, the normal per-project decision db path
    # applies regardless of --db.
    decision_db_arg = getattr(args, "decision_db", None)
    if decision_db_arg:
        md_db_path = Path(decision_db_arg).expanduser().resolve()
    else:
        md_base = Path(os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum")))
        md_db_path = md_base / f"{args.project}.sqlite"
    # F1 (ruling 68): the decision-attach lookup is a decision-index READER
    # (Codex named it explicitly) -- open_db_noncreating replaces the
    # exists()-then-open_db TOCTOU pattern; it already returns None when the
    # file doesn't exist, so no separate exists() check is needed.
    md_conn = None
    try:
        md_conn = open_db_noncreating(md_db_path, project=args.project)
    except sqlite3.DatabaseError:
        md_conn = None
    except DbProjectMismatchError as e:
        # Finding 2: a same-file, different-project decision db is a
        # real misconfiguration, but concept attachment is an
        # enrichment, not the point of this command -- degrade to
        # "no attach" + a named warning, never kill the whole search
        # over it (unlike a direct `open_db` call elsewhere, which
        # should hard-refuse).
        print(f"code-search: WARNING decision db not attached: {e}", file=sys.stderr)
        md_conn = None

    out = []
    for chunk_id, score in results:
        row = conn.execute("SELECT * FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if row is None:
            continue
        hit_root = row["code_root"]
        hit = {
            "path": row["path"],
            "code_root": hit_root,
            "line": row["start_line"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "signature": row["signature"],
            "score": score,
        }
        # Task 6: concept attachment is root-checked -- a hit whose
        # relative path no longer exists under the root it was indexed
        # from must never attach a concept keyed on that path alone. This
        # changes behavior whenever a hit's file is gone from its root --
        # single-root or multi-root alike, heal off in both cases; the
        # adversary that motivated it is specifically two roots sharing
        # one relative path with only one of them still holding the file
        # (concept attachment could otherwise pick the wrong root's
        # match), but a single-root project can drift the exact same way.
        if md_conn is not None and (Path(hit_root) / row["path"]).exists():
            # Finding 4: prefer a symbol-level implemented_by/tested_by
            # match over a file-level one for this specific chunk.
            concept_matches = concept_matches_for_chunk(
                md_conn, args.project, row["path"], row["symbol"], row["qualified_name"]
            )
            if concept_matches:
                hit["concept_id"] = concept_matches[0]["id"]
                hit["concept_title"] = concept_matches[0]["title"]
        out.append(hit)

    if md_conn is not None:
        md_conn.close()
    conn.close()

    if args.json:
        # Finding 1 + Anatomy M2a Task 4: state + full per-root provenance
        # surface here alongside `results` -- an envelope, not a bare
        # list, so a caller can tell "current" apart from "stale" apart
        # from an empty-but-healthy result without a separate call.
        # code_root/indexed_at/head_sha name the FIRST root (code_meta's
        # own alphabetical order), kept for existing readers; code_roots
        # carries the report's full per-root list.
        first_root = report["roots"][0]
        print(json.dumps(
            {
                "state": state,
                "code_root": first_root["code_root"],
                "code_roots": report["roots"],
                "indexed_at": first_root["last_indexed_at"],
                "head_sha": first_root["head_sha"],
                "changed": report["changed"],
                "failed": report["failed"],
                "not_indexed": report["not_indexed"],
                "embedding_mode": report["embedding_mode"],
                "results": out,
            },
            indent=2,
        ))
    else:
        # Task 6: a path alone is ambiguous once a project has more than
        # one code root (the SAME relative path can be indexed under
        # each) -- qualify it with the root only when that ambiguity can
        # actually arise, so the common single-root case keeps its
        # existing, unqualified line.
        multi_root = len(report["roots"]) > 1
        for h in out:
            extra = f"  [{h['concept_id']}]" if "concept_id" in h else ""
            loc = f"{h['code_root']}/{h['path']}:{h['line']}" if multi_root else f"{h['path']}:{h['line']}"
            print(f"{h['score']:.4f}  {loc}  {h['qualified_name']}  {h['signature']}{extra}")
    return 0


# ---------------------------------------------------------------------------
# stats -- the liveness metric (backlog SS2 / INC-0103 / INC-0105)
# ---------------------------------------------------------------------------
#
# INC-0103 (forced retrieval dead on arrival) and INC-0105 (the write-side
# nudge misrouted a whole day of rulings into the wrong folder) share one
# root cause: every hook fails open by design, and there was no liveness
# signal -- a dead hook and a healthy hook that found nothing look
# identical from inside a session. `stats` reads hook.log (never writes
# anything) and reports, per project, whether the read side (pre-edit
# lookups) and the write side (nudges -> actual store commits) are alive.
#
# hook.log line shapes this parser has to handle, none of them optional:
#   <ts> userprompt outcome=X ... session=S project=P
#   <ts> ledger outcome=appended kind=code|store ... session=S project=P
#   <ts> sessionstart outcome=X ... session=S source=... project=P
#   <ts> sessionend outcome=X session=S project=P
#   <ts> precompact outcome=X session=S project=P
#   <ts> newfile-nudge outcome=X project=P file=...
#   <ts> outcome=matched elapsed=Ns project=P file=...      (pre-edit-chain.sh --
#        no hook-type keyword; own independent logger, distinguished below)
#   payload_keys=... project=P                               (no timestamp --
#        a KNOWN timestamp-less shape, counted separately, see
#        untimestamped_lines below)
#   <ts> outcome=watchdog-killed hook=<name> project=P        (mc-watchdog.sh's
#        own expiry line; round-2 review fix -- now carries an offset-bearing
#        timestamp and project= (or the literal "(pre-resolution)" when the
#        env didn't have MEMCONTINUUM_PROJECT set yet at kill time))
#   <ts> memlib: no python resolved (...) project=P
#   <ts> pre-edit-chain: no python resolved (...) project=P
#   <ts> post-commit-reindex: MEMCONTINUUM_ROOT not set, skipping project=P
#   arbitrary python tracebacks / stray stderr landing via `2>>"$MC_LOG"`
#   Tue Sep  2 02:10:00 EDT 2026 ...                          (BSD/macOS
#        `date` fallback shape when `date -Iseconds` isn't supported --
#        this repo's documented bash-3.2/macOS port target. Parsed as a
#        LOCAL NAIVE timestamp, localized to whatever machine runs `stats`
#        itself -- see _parse_bsd_date_prefix. Without this, every real
#        line on such a host was unparseable, which is itself an
#        INC-0103-class silence of the metric.)
#
# Rule (never raise on any of the above): the line must start with either
# an ISO-with-offset timestamp (as the first whitespace token) or the BSD
# `date` fallback's multi-token prefix, else the whole line is silently
# skipped from every count -- this is what makes the payload_keys=/
# watchdog-killed/traceback shapes above harmless instead of crashes. A
# line without `project=` is bucketed under the literal project name
# "(unknown)" rather than dropped, so pre-fix history (or any other logger
# that never learns project=) still shows up somewhere instead of silently
# vanishing from every count -- but neither FLAG is ever evaluated for
# that bucket (round-2 review ruling: attribution there is incomplete by
# design, so "read side silent" there would be a guaranteed false alarm on
# every deployment's cold-start window, not a real signal).
_HOOK_LOG_KEYWORDS = (
    "userprompt", "ledger", "sessionstart", "sessionend", "precompact",
    "newfile-nudge",
)
UNKNOWN_STATS_PROJECT = "(unknown)"

# Round 2 (Codex gate, item 8) + round-3 addendum (review finding): every
# outcome userprompt-remind.sh's finish() can ever log, and whether it
# means a confirmed, distinct, live user turn actually happened:
#
#   EXCLUDED (does not confirm a real user turn -- a malformed/ineligible
#   delivery, evaluated BEFORE the hook can even establish which session
#   this is or that it's a real main-thread turn):
#     empty-payload   -- no payload at all; nothing to process
#     no-session-id   -- payload present but no session_id; can't even
#                        say WHICH session this belongs to
#     no-state        -- session_id present but no state file for it (no
#                        prior SessionStart) -- no turn tracking, no
#                        evidence considered, functionally unprocessed
#     agent-source    -- a subagent/persona run, not the main user thread
#     duplicate-delivery -- a redelivered prompt_id, not a NEW turn
#     non-user-source -- the pre-INC-0103-fix gate's own dead outcome
#                        name (kept only because OLD hook.log lines can
#                        still carry it)
#   COUNTED (everything past those gates: this outcome is only reachable
#   once the hook has already confirmed a real session_id, existing
#   state, and a non-agent turn -- a downstream failure past that point
#   is the HOOK's own infra choking on a confirmed real prompt, not
#   evidence the prompt wasn't real; excluding these would UNDER-count
#   genuine engagement and could itself hide a real INC-0103-class
#   silence, e.g. every real turn failing at mktemp):
#     mktemp-failed, decision-failed, injected, no-evidence,
#     lookback-injected
#
# Excluded from `user_prompts` so ten of THESE alone can never satisfy
# the read-side FLAG's ">=10 prompts" busy-signal on their own -- they
# prove the hook ran, not that a human was actively prompting.
_NON_USER_PROMPT_OUTCOMES = frozenset({
    "duplicate-delivery", "agent-source", "non-user-source",
    "empty-payload", "no-session-id", "no-state",
})

_MONTH_ABBR = {
    name: i for i, name in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}
# `%a %b %e %H:%M:%S %Z %Y` -- BSD/macOS `date`'s default output (what
# every `date -Iseconds 2>/dev/null || date` fallback line becomes on a
# host without GNU date's -I flag). %e is space-padded (` 2`, not `02`),
# hence ` +` rather than a fixed width; the zone abbreviation (%Z, e.g.
# EDT/PST/UTC) is matched but deliberately NOT captured -- see
# _parse_bsd_date_prefix for why.
_BSD_DATE_RE = re.compile(
    r"^[A-Za-z]{3} ([A-Za-z]{3}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2}) [A-Za-z]{2,5} (\d{4})"
)


def _parse_hook_log_ts(token: str):
    """ISO-with-offset only (`date -Iseconds`'s own format, e.g.
    2026-09-01T12:04:12-04:00). Returns None for anything else: a naive
    timestamp (no offset -- can't be compared to `now` safely), a bare word
    like `payload_keys=...` or a python traceback's first token, etc."""
    try:
        ts = datetime.fromisoformat(token)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        return None
    return ts


def _parse_bsd_date_prefix(line: str):
    """Round-2 review finding: the BSD/macOS `date` fallback (no
    `-Iseconds` support) prints `Tue Sep  2 02:10:00 EDT 2026`, not an
    ISO offset -- every real hook.log line on such a host was previously
    100% unparseable, which is itself the exact class of silent failure
    this metric exists to catch. Returns (naive_datetime, matched_length)
    or (None, 0). The zone abbreviation is matched but discarded: Python's
    stdlib cannot reliably resolve an arbitrary three-to-five-letter zone
    abbreviation (EDT, PST, ...) to a UTC offset (there is no
    installation-independent abbreviation->offset table), so the returned
    datetime is NAIVE -- the caller localizes it to whatever machine is
    running `stats` via `.astimezone()`, on the accepted assumption that a
    single-operator liveness tool reads logs on the same machine (or at
    least the same timezone) that wrote them."""
    m = _BSD_DATE_RE.match(line)
    if not m:
        return None, 0
    mon_abbr, day, hh, mm, ss, year = m.groups()
    month = _MONTH_ABBR.get(mon_abbr)
    if month is None:
        return None, 0
    try:
        dt = datetime(int(year), month, int(day), int(hh), int(mm), int(ss))
    except ValueError:
        return None, 0
    return dt, m.end()


def _parse_hook_log_line_ts(line: str):
    """Returns (ts_aware_or_None, rest_of_line). Two timestamp shapes are
    recognized -- ISO-with-offset as the whole first whitespace token, or
    the BSD `date` fallback's multi-token prefix (see
    _parse_bsd_date_prefix, localized to this process's own timezone via
    `.astimezone()`, which -- called with no argument on a NAIVE datetime
    -- attaches the system's local UTC offset without altering the
    wall-clock fields, i.e. exactly "this naive value IS local time").
    Neither shape recognized: ts is None and rest is the line unchanged
    (nothing meaningful to split off)."""
    first, _, remainder = line.partition(" ")
    ts = _parse_hook_log_ts(first)
    if ts is not None:
        return ts, remainder
    naive, end = _parse_bsd_date_prefix(line)
    if naive is not None:
        try:
            return naive.astimezone(), line[end:].lstrip(" ")
        except Exception:
            return None, line
    return None, line


def _hook_log_line_kind(rest: str) -> str:
    """Classify a hook.log line's PRODUCER by shape, never by content.
    `rest` is everything after the leading timestamp token.

    pre-edit-chain.sh's finish() is the only producer whose line starts
    with a bare `outcome=` (no hook-type keyword) AND carries `elapsed=`
    right after it -- `outcome=$outcome elapsed=${elapsed}s project=...
    file=...`. memlib.sh's OWN raw outcome lines (mc_update_state_json's
    lock-timeout/lock-open-failed/state-dir-failed, logged via mc_log with
    no hook-type prefix either) never carry `elapsed=` -- without that
    second check those would misclassify as pre-edit lookups and silently
    suppress the INC-0103 FLAG (a real false negative, not a cosmetic
    miscount)."""
    stripped = rest.strip()
    if stripped.startswith("outcome=") and " elapsed=" in stripped:
        return "pre-edit"
    for kw in _HOOK_LOG_KEYWORDS:
        if stripped.startswith(kw + " ") or stripped == kw:
            return kw
    return "other"


# Every hook-log "kind" _hook_log_line_kind can return, pre-seeded so a
# report always has a Counter to read from even for a kind this window
# never saw (never a KeyError, never a silent `.get(..., {})` fallback).
_STATS_KINDS = (
    "userprompt", "ledger", "pre-edit", "newfile-nudge",
    "sessionstart", "sessionend", "precompact", "other",
)


def _new_stats_bucket():
    return {
        "sessions": set(),
        "lines": 0,
        # user_prompts is NOT tracked as a separate counter (round 2,
        # Codex gate item 8): it is a VIEW over outcomes["userprompt"] at
        # report time, excluding _NON_USER_PROMPT_OUTCOMES -- see
        # _stats_report. A separate increment here would double the
        # bookkeeping this dict already exists to replace.
        #
        # Fix round 1 (review finding, MINOR): outcome counts are tallied
        # DYNAMICALLY per kind -- {kind: Counter(outcome -> count)} -- so no
        # outcome value is ever silently folded into a total or dropped,
        # whether or not this file's own named metrics (pre_edit.other,
        # newfile_nudge's four named outcomes, etc.) happen to enumerate it.
        # Every named field the report prints is a VIEW computed from this
        # dict at report time (see _stats_report), never a separate
        # incremented-in-the-loop counter -- so a brand-new outcome string
        # some future hook edit introduces shows up in `outcomes` on the
        # very next run with no code change here.
        "outcomes": {k: Counter() for k in _STATS_KINDS},
    }


_FIELD_RE = re.compile(r"(?:^|\s)(\w+)=(\S*)")


_TRAILING_PROJECT_TOKEN_RE = re.compile(r"^project=([A-Za-z0-9._-]+)$")


def _hook_log_fields(rest: str) -> dict:
    """Every producer writes space-separated `key=value` tokens (a few
    values may be empty, e.g. `session=` on a payload missing session_id).
    A generic key=value scan is more robust than one regex per field name,
    and costs nothing extra -- every line here is short.

    Round-2 Codex gate finding: `file=`'s value is an arbitrary filesystem
    path, which can itself contain a substring shaped like `key=value`
    (reproduced: a path literally containing `project=other`, e.g.
    `/tmp/a project=other/x.py`). Without care, the generic scan's
    last-match-wins semantics can let that embedded text overwrite the
    line's REAL project=.

    Round 3 (review fix): round 2 shipped a KIND-AWARE fix (special-cased
    `kind == "ledger"`) that was itself incomplete -- memlib.sh's own raw
    diagnostic lines (mc_log's `outcome=lock-timeout file=$lockfile` /
    `lock-open-failed` / `state-dir-failed`) ALSO go through mc_log, which
    ALWAYS appends `project=$MC_PROJECT` as the line's unconditional last
    token, but those lines classify as kind "other" (no hook-type
    keyword, and no `elapsed=` either -- see _hook_log_line_kind), so the
    round-2 fix missed them entirely: their trailing project= was
    silently swallowed into the file value and the line landed in
    "(unknown)". The rule is STRUCTURAL, not per-kind: mc_log's guarantee
    ("project= is always this line's last token") holds regardless of
    WHICH kind of line it's logging for, so there is no need to enumerate
    kinds at all -- check the tail's (everything after the first
    ` file=`) own LAST whitespace-delimited token, unconditionally, for
    every kind.

    One deliberate refinement on top of the literal round-3 review
    wording: the last token must look like a REAL project value, not
    merely `\\S+`. MemContinuum project names are already restricted
    elsewhere in this codebase to `[A-Za-z0-9._-]+` (repo-init.sh refuses
    anything else, e.g. the skill's `--project NAME` doc). Restricting the
    value to that charset (no `/`) rejects a file path fragment like
    "other/x.py" while still accepting every genuine mc_log project
    append (always a valid project name).

    Round 4 (review fix -- round 3's universal tail-rescan was itself a
    NEW hijack): applying the tail rescan UNCONDITIONALLY, even when the
    prefix already found a real project=, let a pre-edit-chain.sh/
    newfile-nudge.sh line (their genuine shape: `... project=REAL
    file=F`, project= BEFORE file=, nothing structurally follows file=)
    get overwritten by an EDITED FILE whose path happens to end in
    ` project=validname` -- reproduced: `... project=realproj
    file=/nowhere/near/anything project=validname` re-attributed the
    whole line to "validname", not "realproj". The charset restriction
    above stops an adversarial "/other/x.py" shape but does nothing
    against an adversarial shape using ONLY valid project-name
    characters.

    Fixed with a strict precedence rule, not another charset tweak: the
    tail is rescanned ONLY when the PREFIX (everything before the first
    ` file=`) found NO project= of its own. If the prefix already has
    one, it is final -- full stop, the tail is never even looked at. This
    makes both real shapes provably safe simultaneously:
      - pre-edit-chain.sh / newfile-nudge.sh (`project=P file=F`,
        project= always in the prefix): the prefix always has project=,
        so the tail rescan never runs for these lines AT ALL -- no file
        value, however constructed, can ever change their project=.
      - every mc_log-sourced line (ledger-post-edit.sh, memlib.sh's own
        raw diagnostics, userprompt/sessionstart/sessionend/precompact):
        their own hook-specific message text never mentions "project="
        itself, so the prefix never has one, and the tail rescan always
        runs -- exactly where it needs to, since mc_log's real project=
        append is genuinely the tail's last token there.
    The one remaining, accepted ambiguity is therefore narrower than
    round 3's version: a LEGACY, pre-project=-fix mc_log-sourced line (no
    prefix project=, by definition -- old mc_log never appended one)
    whose FILE VALUE itself happens to end in a valid-charset
    ` project=X` with nothing genuine following it. Lexically
    indistinguishable from a real current line logging a boring file
    named X with a real trailing project=X append; resolved in favor of
    attribution (treated as the latter) since it is the far more common
    case and the coincidence required for the former is rare."""
    marker = " file="
    idx = rest.find(marker)
    if idx == -1:
        return {k: v for k, v in _FIELD_RE.findall(rest)}
    prefix = rest[:idx]
    fields = {k: v for k, v in _FIELD_RE.findall(prefix)}
    tail = rest[idx + len(marker):]

    if "project" not in fields:
        last_sep = tail.rfind(" ")
        last_token = tail[last_sep + 1:]
        m = _TRAILING_PROJECT_TOKEN_RE.match(last_token)
        if m:
            fields["project"] = m.group(1)
            tail = tail[:last_sep] if last_sep != -1 else ""

    fields["file"] = tail
    return fields


def _scan_hook_log(log_path: Path, cutoff: datetime, now: datetime):
    """Returns (buckets: {project: bucket}, unknown_lines: int,
    projects_seen: set[str], unparseable_lines: int, untimestamped_lines:
    int). Never raises: an unreadable file, a non-UTF-8 byte, or any
    single malformed line is tolerated -- this function's caller
    (cmd_stats) still wraps the whole thing in case a genuinely unexpected
    failure shows up, per this repo's fail-open rule for every hook.log
    consumer.

    `unparseable_lines` is this metric's OWN self-liveness signal (a
    reviewer finding, not in the original spec): a line whose leading
    token(s) do not parse as either an ISO-with-offset timestamp or the
    BSD `date` fallback shape (see _parse_hook_log_line_ts) is silently
    skipped from every count. That silence would hide a genuinely broken
    host -- e.g. hook.log filling up with stray stderr or python
    tracebacks -- as a healthy-looking all-zero stats output,
    indistinguishable from a dead system. Counting it separately (never
    silently) means an operator staring at zero counts can tell "nothing
    happened" apart from "the metric itself can't read this log".

    `untimestamped_lines` is the round-2 refinement of that signal
    (reviewer finding): `payload_keys=...` lines are a KNOWN,
    by-design-timestamp-less shape (userprompt-remind.sh's payload-shape
    capture writes them directly, not through mc_log) -- counting them
    under `unparseable_lines` would make that number tick on every single
    healthy run, burying the ratio's only real discriminator ("this is
    new/unexpected breakage") under permanent, harmless noise. They are
    counted here instead, separately."""
    buckets: dict[str, dict] = {}
    unknown_lines = 0
    unparseable_lines = 0
    untimestamped_lines = 0
    projects_seen: set[str] = set()

    try:
        raw_lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        # Fix round 1 (review finding, IMPORTANT; historical -- at the
        # time, this function returned a 4-tuple): this branch used to
        # return a 5-tuple (a duplicated unparseable_lines) while the
        # normal path below returned 4, and cmd_stats always unpacked 4 --
        # an exists-but-unreadable hook.log (permissions, a mid-rotation
        # window, anything read_text can raise OSError for) blew up with
        # "too many values to unpack" INSIDE the try/except that is
        # supposed to make this tool fail open, printing "stats: internal
        # error (...)" instead of a real message -- the exact kind of
        # silent-failure-about-silent-failure this metric exists to catch.
        # Reproduced: chmod 000 an existing hook.log. Round 2 later added
        # a genuine 5th return value (untimestamped_lines) to BOTH
        # branches -- kept in sync here on purpose; this comment is a
        # trip-wire for the next person editing either branch alone.
        return buckets, unknown_lines, projects_seen, unparseable_lines, untimestamped_lines

    for line in raw_lines:
        if not line.strip():
            continue
        ts, rest = _parse_hook_log_line_ts(line)
        if ts is None:
            if line.lstrip().startswith("payload_keys="):
                untimestamped_lines += 1
            else:
                unparseable_lines += 1
            continue
        if ts < cutoff or ts > now:
            continue
        kind = _hook_log_line_kind(rest)
        fields = _hook_log_fields(rest)
        project = fields.get("project") or UNKNOWN_STATS_PROJECT
        projects_seen.add(project)
        if project == UNKNOWN_STATS_PROJECT:
            unknown_lines += 1

        bucket = buckets.setdefault(project, _new_stats_bucket())
        bucket["lines"] += 1

        outcome = fields.get("outcome", "")

        if kind == "sessionstart":
            session = fields.get("session")
            if session:
                bucket["sessions"].add(session)
        elif kind == "ledger":
            # Fix round 1: the code/store split is a SEPARATE field
            # (`kind=`), not the outcome itself -- folded into the outcome
            # key here (rather than a second fixed-list branch) so the
            # dynamic outcomes dict still carries the full, undivided
            # picture: any ledger outcome OTHER than "appended" (e.g.
            # out-of-scope, no-session-id, update-failed) is tallied under
            # its own literal string, never silently dropped.
            outcome_key = outcome
            if outcome == "appended":
                outcome_key = f"appended:{fields.get('kind') or 'unknown'}"
            bucket["outcomes"]["ledger"][outcome_key] += 1
            continue
        bucket["outcomes"][kind][outcome] += 1
        # userprompt: `user_prompts` is derived at report time from this
        # same outcomes["userprompt"] Counter (round 2, item 8) -- no
        # separate increment here. sessionend/precompact/other: counted in
        # `lines` and their own `outcomes[kind]` bucket; no dedicated named
        # metric asked for by the spec, but nothing here is silently
        # dropped either.

    return buckets, unknown_lines, projects_seen, unparseable_lines, untimestamped_lines


def _count_store_commits(store_dir: str, cutoff: datetime, now: datetime):
    """`git -C STORE log --since=<cutoff> --until=<now> --oneline` line
    count. None (not 0) on any failure -- STORE not a repo, git missing,
    timeout -- so the caller can tell "unmeasured" apart from "measured
    zero".

    Round 2 (Grok gate, ruling 1 + Codex gate, item 11): this NO LONGER
    gates the write-side FLAG (see _stats_report) -- INC-0105's own
    numbers proved why: a single unrelated commit anywhere in the window
    (a README fix, an unrelated project's ruling, the store's own
    relocation) made `store_commits >= 1` and hid a genuinely silent day.
    It is reported as CORROBORATION ONLY now, alongside the FLAG-driving
    ledger store-kind append count. Two fixes to the measurement itself:
    `--until=<now>` bounds the window on BOTH ends (a future-dated commit
    -- clock skew, a rebase, a deliberately backdated one -- used to still
    count as "in the window" with no upper bound at all); and the count is
    explicitly STORE-WIDE, not scoped to any one project (a store repo can
    be shared by more than one project's wiring), which is exactly why it
    can only ever corroborate, never veto."""
    try:
        result = subprocess.run(
            ["git", "-C", str(store_dir), "log",
             f"--since={cutoff.isoformat()}", f"--until={now.isoformat()}", "--oneline"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return len([l for l in result.stdout.splitlines() if l.strip()])


def _stats_report(
    args, buckets, unknown_lines, projects_seen, now, cutoff, store_commits,
    unparseable_lines, untimestamped_lines,
):
    """Every named field below (pre_edit.matched, newfile_nudge.nudged,
    nudges.coverage_injected, ...) is a VIEW computed here, at report time,
    over the bucket's dynamic `outcomes[kind]` Counter -- never a separate
    counter incremented in the scan loop. Fix round 1 (review finding,
    MINOR): the old code kept a hand-picked fixed list of outcome names per
    kind (four newfile-nudge outcomes out of the thirteen that hook can
    actually log, "other" as a single bucket losing which pre-edit outcome
    it actually was) -- any outcome string not on that list was silently
    invisible. Each named metric's raw Counter is also exposed under an
    "outcomes" key in the result, so no outcome is ever silently folded
    away: a brand-new outcome value some future hook edit introduces shows
    up there on the very next run with no code change here.

    Round 2 (Grok + Codex gates) FLAG redesign:
      - write-side FLAG is keyed on `ledger_appends.store` (this project's
        own per-day-accurate append ledger), NEVER on `store_commits` (a
        store-wide git count reported only as corroboration) -- ruling 1.
        INC-0105 itself is the proof: 12 nudges, 0 store-kind ledger
        appends that day, but an unrelated store commit elsewhere in the
        window would have hidden it under the old git-gated formula.
      - read-side FLAG's "lookups" means real for-path EXECUTIONS only --
        `pre_edit.matched + pre_edit.no_match` -- never `pre_edit.other`
        (index-missing, no-file-path, output-build-failed, ...), which
        counts FAILED-OR-NEVER-ATTEMPTED lookups and must not read as
        evidence the read side is alive (Codex gate item 7).
      - `user_prompts` excludes _NON_USER_PROMPT_OUTCOMES (duplicate
        redeliveries, subagent/persona runs, the dead pre-fix gate outcome)
        so those alone can never fake the ">=10 prompts" busy signal
        (Codex gate item 8).
      - NEITHER flag is ever evaluated for the "(unknown)" project: lines
        with no project= are bucketed there so they are never silently
        dropped, but attribution IS incomplete there by construction (a
        mix of every un-projected logger, forever, not one coherent
        project's history) -- "read side silent" on that bucket would be a
        guaranteed false alarm on every deployment's cold-start window,
        not a real signal (Grok gate ruling 2)."""
    b = buckets.get(args.project, _new_stats_bucket())
    outcomes = b["outcomes"]

    pe = outcomes["pre-edit"]
    pre_edit_total = sum(pe.values())
    pre_edit_matched = pe.get("matched", 0)
    pre_edit_no_match = pe.get("no-match", 0)
    pre_edit_other = pre_edit_total - pre_edit_matched - pre_edit_no_match
    pre_edit_lookups = pre_edit_matched + pre_edit_no_match

    led = outcomes["ledger"]
    ledger_code = led.get("appended:code", 0)
    ledger_store = led.get("appended:store", 0)

    up = outcomes["userprompt"]
    coverage_injected = up.get("injected", 0)
    lookback_injected = up.get("lookback-injected", 0)
    no_evidence = up.get("no-evidence", 0)
    duplicate_delivery = up.get("duplicate-delivery", 0)
    nudges_total = coverage_injected + lookback_injected
    non_user_prompt_lines = sum(up.get(k, 0) for k in _NON_USER_PROMPT_OUTCOMES)
    user_prompts = sum(up.values()) - non_user_prompt_lines

    nf = outcomes["newfile-nudge"]
    nf_nudged = nf.get("nudged", 0)
    nf_not_indexed = nf.get("not-indexed-extension", 0)
    nf_lang_not_wired = nf.get("language-available-not-wired", 0)
    nf_never = nf.get("never-extension", 0)

    flags = []
    if args.project != UNKNOWN_STATS_PROJECT:
        if nudges_total >= 3 and ledger_store == 0:
            flags.append(
                f"FLAG: write side silent — {nudges_total} reminders fired, "
                f"nothing appended to the store in {args.days}d"
            )
        if user_prompts >= 10 and pre_edit_lookups == 0:
            flags.append(
                f"FLAG: read side silent — {user_prompts} prompts, no "
                f"retrieval matched or missed in {args.days}d"
            )

    result = {
        "project": args.project,
        "days": args.days,
        "window_start": cutoff.isoformat(),
        "window_end": now.isoformat(),
        "sessions_seen": len(b["sessions"]),
        "user_prompts": user_prompts,
        "non_user_prompt_lines": non_user_prompt_lines,
        "pre_edit": {
            "matched": pre_edit_matched,
            "no_match": pre_edit_no_match,
            "other": pre_edit_other,
            "total": pre_edit_total,
            "lookups": pre_edit_lookups,
            "outcomes": dict(pe),
        },
        "ledger_appends": {
            "code": ledger_code,
            "store": ledger_store,
            "outcomes": dict(led),
        },
        "nudges": {
            "coverage_injected": coverage_injected,
            "lookback_injected": lookback_injected,
            "no_evidence": no_evidence,
            "duplicate_delivery": duplicate_delivery,
            "total": nudges_total,
            "outcomes": dict(up),
        },
        "newfile_nudge": {
            "nudged": nf_nudged,
            "not_indexed_extension": nf_not_indexed,
            "language_available_not_wired": nf_lang_not_wired,
            "never_extension": nf_never,
            "outcomes": dict(nf),
        },
        "store_commits": store_commits,
        "unknown_lines": unknown_lines,
        "unparseable_lines": unparseable_lines,
        "untimestamped_lines": untimestamped_lines,
        "projects_seen": sorted(projects_seen),
        "flags": flags,
    }
    return result


def cmd_stats(args) -> int:
    """`memidx.py stats --project P [--days N] [--home DIR] [--store DIR]
    [--json]` -- the liveness metric (backlog SS2, INC-0103/INC-0105): reads
    hook.log and reports, per project, whether the read side (real pre-edit
    lookups) and the write side (nudges -> this project's own store-kind
    ledger appends) are alive. Never touches hook.log or anything else --
    read-only, exit 0 always (missing hook.log, unreadable file, a bad
    --store, anything: this prints one line and returns 0, on the same
    fail-open principle every hook in this repo already follows -- a
    broken liveness check must never itself become a second silent
    failure mode).

    --store is optional and, since round 2, never gates either FLAG: it
    adds a store-wide git-commit count reported purely as corroboration
    (`store_commits`) alongside the FLAG-driving ledger numbers. Neither
    FLAG is ever raised for `--project '(unknown)'` -- see _stats_report.
    """
    # Everything -- including the final print block -- lives inside this one
    # try/except: a printing failure (e.g. a non-UTF-8 stdout choking on the
    # —/→ glyphs below) must fail open exactly like a parsing failure would,
    # never a traceback/exit 1 from what is supposed to be the tool that
    # catches silent failures.
    try:
        home = Path(args.home).expanduser() if args.home else Path(
            os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum"))
        )
        log_path = home / "hook.log"

        if args.now:
            now = _parse_hook_log_ts(args.now) or datetime.now(timezone.utc)
        else:
            now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=args.days)

        if not log_path.exists():
            print(f"no hook.log at {log_path}")
            return 0

        # Fix round 1 (review finding): an EXISTING-but-unreadable
        # hook.log (permissions, a mid-rotation window) is a distinct,
        # tolerant case from "no hook.log at all" -- it gets its own
        # message rather than silently falling through to an all-zero
        # report indistinguishable from "nothing happened", and rather
        # than the internal-error path this cheap open+close probe
        # exists specifically to avoid. _scan_hook_log's own OSError
        # branch (see its comment) is a defensive fallback for the TOCTOU
        # gap between this check and the real read, not the primary path.
        try:
            with open(log_path, "r"):
                pass
        except OSError as e:
            print(f"hook.log exists but is unreadable at {log_path} ({e})")
            return 0

        buckets, unknown_lines, projects_seen, unparseable_lines, untimestamped_lines = _scan_hook_log(
            log_path, cutoff, now
        )

        store_commits = None
        if args.store:
            store_commits = _count_store_commits(args.store, cutoff, now)

        result = _stats_report(
            args, buckets, unknown_lines, projects_seen, now, cutoff, store_commits,
            unparseable_lines, untimestamped_lines,
        )

        if args.json:
            print(json.dumps(result, indent=2))
            return 0

        print(f"MemContinuum liveness stats — project={result['project']} days={result['days']}")
        print(f"window: {result['window_start']} .. {result['window_end']}")
        print()
        print(f"sessions seen: {result['sessions_seen']}")
        print(
            f"user prompts: {result['user_prompts']}"
            + (
                f"  ({result['non_user_prompt_lines']} duplicate/agent/non-user lines excluded)"
                if result["non_user_prompt_lines"] else ""
            )
        )
        print()
        pe = result["pre_edit"]
        print(f"pre-edit lookups: matched={pe['matched']} no-match={pe['no_match']} "
              f"(real lookups {pe['lookups']}); other={pe['other']} "
              f"(failed/never-attempted, not counted as a lookup) -- total lines {pe['total']}")
        la = result["ledger_appends"]
        print(f"ledger appends: code={la['code']} store={la['store']}")
        print()
        nu = result["nudges"]
        print(f"write-side nudges: coverage-injected={nu['coverage_injected']} "
              f"lookback-injected={nu['lookback_injected']} no-evidence={nu['no_evidence']} "
              f"duplicate-delivery={nu['duplicate_delivery']}")
        nf = result["newfile_nudge"]
        print(f"new-file nudges: nudged={nf['nudged']} "
              f"not-indexed-extension={nf['not_indexed_extension']} "
              f"language-available-not-wired={nf['language_available_not_wired']} "
              f"never-extension={nf['never_extension']}")
        print()
        if result["store_commits"] is None:
            print("store commits (all projects, corroboration only) in window: not measured (pass --store to measure)")
        else:
            print(f"store commits (all projects, corroboration only) in window: {result['store_commits']}")
        print(f"nudges → store-kind ledger appends (this project, drives the FLAG below): "
              f"{nu['total']} → {la['store']}")
        for flag in result["flags"]:
            print(flag)
        print()
        if result["unknown_lines"]:
            print(
                f'note: {result["unknown_lines"]} legacy lines without project= are bucketed '
                f'under "(unknown)" — counts there are incomplete by design'
            )
        if result["unparseable_lines"]:
            print(
                f"unparseable lines skipped (no ISO-with-offset or BSD-date timestamp -- "
                f"self-liveness signal, see --help): {result['unparseable_lines']}"
            )
        if result["untimestamped_lines"]:
            print(f"untimestamped lines skipped (known shape, e.g. payload_keys=...): {result['untimestamped_lines']}")
        if result["projects_seen"]:
            print(f"projects seen in window: {', '.join(result['projects_seen'])}")
        return 0
    except Exception as e:  # fail-open: a broken liveness check is not
        # allowed to become a second silent failure mode.
        print(f"stats: internal error ({e}) -- exit 0 (fail-open)")
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_args(p: argparse.ArgumentParser, need_root: bool = False) -> None:
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--db", default=None, help="override the index DB path")
    if need_root:
        p.add_argument("--root", required=True, help="markdown root to walk")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="memidx.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_reindex = sub.add_parser("reindex")
    add_common_args(p_reindex, need_root=True)
    p_reindex.add_argument("--full", action="store_true")
    p_reindex.add_argument("--no-embed", action="store_true")
    p_reindex.add_argument(
        "--auto", action="store_true",
        help="internal/hook use: reindex content without embedding; embedding_mode is left "
             "exactly as it was ONLY when this pass changes nothing, and recomputed from real "
             "coverage (never re-embedding) whenever it does add/change/remove a row -- so "
             "'full' never keeps standing over a vector this same pass just made stale",
    )
    p_reindex.set_defaults(func=cmd_reindex)

    p_search = sub.add_parser("search")
    add_common_args(p_search)
    p_search.add_argument("query")
    p_search.add_argument("--mode", choices=["fts", "vector", "hybrid"], default="hybrid")
    p_search.add_argument("--status", action="append", default=[])
    p_search.add_argument("--type", action="append", default=[])
    p_search.add_argument("--area", default=None)
    p_search.add_argument("--topic", default=None)
    p_search.add_argument("--authority", default=None)
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_chain = sub.add_parser("chain")
    add_common_args(p_chain)
    p_chain.add_argument("topic")
    p_chain.add_argument("--json", action="store_true")
    p_chain.set_defaults(func=cmd_chain)

    p_forpath = sub.add_parser("for-path")
    add_common_args(p_forpath)
    p_forpath.add_argument("file_path")
    p_forpath.add_argument("--json", action="store_true")
    p_forpath.set_defaults(func=cmd_for_path)

    p_check = sub.add_parser("check")
    add_common_args(p_check, need_root=True)
    p_check.add_argument("--json", action="store_true")
    p_check.set_defaults(func=cmd_check)

    p_why = sub.add_parser("why")
    add_common_args(p_why)
    p_why.add_argument("symbol_or_path")
    p_why.add_argument("--code-root", dest="code_root", default=None,
                        help="required only to resolve a bare symbol (no slash)")
    p_why.add_argument("--json", action="store_true")
    p_why.set_defaults(func=cmd_why)

    p_drift = sub.add_parser("drift")
    add_common_args(p_drift)
    p_drift.add_argument("--code-root", dest="code_root", required=True)
    p_drift.add_argument("--json", action="store_true")
    p_drift.add_argument(
        "--strict-holds", action="store_true",
        help="also fail the exit code on a HOLD violation (an active reviewer-finding/"
             "code-derived/agent-inference invariant with validated evidence) -- without "
             "this flag, a HOLD violation is reported but never blocks (SCHEMA.md §4: a "
             "HOLD 'may block', at the caller's discretion)",
    )
    p_drift.set_defaults(func=cmd_drift)

    p_unmapped = sub.add_parser("unmapped")
    add_common_args(p_unmapped, need_root=True)
    p_unmapped.add_argument("paths", nargs="+", metavar="PATH")
    p_unmapped.add_argument("--code-root", dest="code_root", default=None)
    p_unmapped.add_argument("--json", action="store_true")
    p_unmapped.set_defaults(func=cmd_unmapped)

    p_code_reindex = sub.add_parser("code-reindex")
    add_common_args(p_code_reindex)
    p_code_reindex.add_argument("--code-root", dest="code_root", required=False)
    p_code_reindex.add_argument(
        "--drop-root", dest="drop_root", default=None,
        help="remove one code root's rows from the project's index; nothing is walked",
    )
    p_code_reindex.add_argument(
        "--lang", default=None,
        help="comma-separated language filter, e.g. swift,python. Required on a "
             "project's first code-reindex; omit it on later runs to reuse the "
             "langs stored from the first run.",
    )
    p_code_reindex.add_argument("--no-embed", action="store_true")
    p_code_reindex.add_argument(
        "--full", action="store_true",
        help="re-chunk every file under this root even when its content and "
             "chunker version already match the stored ones, and -- combined "
             "with --lang -- allow that flag to drop a language from the "
             "project's stored set instead of refusing (a plain --lang can "
             "only add languages, never remove one)",
    )
    p_code_reindex.add_argument(
        "--no-retry-not-indexed", dest="retry_not_indexed", action="store_false", default=True,
        help="leave not-indexed files alone unless the backend set or the chunker changed "
             "(the heal uses this)",
    )
    p_code_reindex.set_defaults(func=cmd_code_reindex)

    p_code_search = sub.add_parser("code-search")
    add_common_args(p_code_search)
    p_code_search.add_argument("query")
    p_code_search.add_argument("--mode", choices=["fts", "vector", "hybrid"], default="hybrid")
    p_code_search.add_argument("--limit", type=int, default=10)
    p_code_search.add_argument("--json", action="store_true")
    p_code_search.add_argument(
        "--decision-db", default=None,
        help="override the DECISION (markdown) index db path used for concept "
             "attachment -- independent of --db, which always selects the CODE "
             "db for this command. Defaults to the normal per-project decision db.",
    )
    p_code_search.add_argument(
        "--no-heal", dest="no_heal", action="store_true",
        help="skip the automatic repair of a stale or degraded index before searching",
    )
    p_code_search.add_argument(
        "--heal-limit", dest="heal_limit", type=int, default=500,
        help="above this many changed files, skip the automatic repair "
             "entirely and say so instead of attempting it -- an "
             "all-or-nothing gate on the preflight's changed count, not a "
             "per-file cap on what a repair re-indexes; a repair triggered "
             "only by a backend-availability change (no file content "
             "changed) is never subject to this gate",
    )
    p_code_search.set_defaults(func=cmd_code_search)

    p_code_census = sub.add_parser("code-census")
    p_code_census.add_argument("--root", required=True)
    p_code_census.add_argument("--json", action="store_true")
    p_code_census.set_defaults(func=cmd_code_census)

    p_stats = sub.add_parser(
        "stats",
        help="liveness metric from hook.log: per-project read-side (real "
             "pre-edit lookups) and write-side (nudges vs. this project's own "
             "store-kind ledger appends) activity in a trailing window, with "
             "FLAG lines when either side goes silent.",
        description=(
            "Reads $MEMCONTINUUM_HOME/hook.log (never writes anything) and reports, "
            "for one project, whether the read side (real pre-edit lookups) and the "
            "write side (write-side nudges vs. this project's own store-kind ledger "
            "appends) are actually alive in a trailing window: every hook fails "
            "open by design, so a dead hook and a healthy hook that found nothing "
            "look identical from inside a session -- this is the signal that tells "
            "them apart.\n\n"
            "Reports: sessions seen, user prompts (duplicate/subagent/non-user "
            "lines excluded), pre-edit lookups (matched/no-match count as real "
            "lookups; every other pre-edit outcome -- index-missing, no-file-path, "
            "output-build-failed, ... -- is a FAILED or NEVER-ATTEMPTED lookup, "
            "reported under 'other' but never counted as read-side evidence), "
            "ledger appends (code/store), write-side nudges (coverage-injected/"
            "lookback-injected/no-evidence/duplicate-delivery), new-file nudge "
            "outcomes, and a store-wide git commit count (only with --store, "
            "corroboration only -- see below) and two FLAG lines: "
            "'write side silent' (>=3 nudges but 0 store-kind LEDGER appends this "
            "project made -- --store's git count is reported alongside but never "
            "gates this: one unrelated commit anywhere in a shared store must not "
            "hide a genuinely silent day) and 'read side silent' (>=10 real user "
            "prompts but 0 real pre-edit lookups (matched or no-match) -- a run of "
            "failed/never-attempted lookups does not count as evidence the read "
            "side is alive).\n\n"
            "Lines with no project= token (any logger that never learns it) are "
            "grouped under the literal project name '(unknown)' rather than "
            "dropped -- pass --project '(unknown)' to see them, but NEITHER flag "
            "is ever raised for that bucket: its attribution is incomplete by "
            "design (a mix of every un-projected logger, forever), so 'read side "
            "silent' there would be a guaranteed false alarm on every "
            "deployment's cold-start window, not a real signal. Fail-open "
            "throughout: a missing hook.log, an unreadable file, a bad --store "
            "path, or any unexpected error prints one line and exits 0.\n\n"
            "Attribution rule: a trailing project= token after file= (mc_log, "
            "hooks/memlib.sh, always appends one last, for every kind of line "
            "it logs) is rescued ONLY when the text BEFORE file= carries no "
            "project= of its own -- a prefix project= (pre-edit-chain.sh's/ "
            "newfile-nudge.sh's own shape) is always final and the file value "
            "is never rescanned, so an edited file whose path ends in "
            "' project=X' can never re-attribute one of those lines. The one "
            "remaining ambiguity is narrower: a line with no prefix project= "
            "whose FILE PATH itself happens to end in a valid-charset "
            "' project=X' segment with nothing after it is indistinguishable "
            "from a real line naming that same file with a genuine trailing "
            "project=X. Considered rare enough to accept."
        ),
    )
    p_stats.add_argument("--project", default=DEFAULT_PROJECT, help="project namespace to report on (default: %(default)s)")
    p_stats.add_argument("--days", type=int, default=7, help="trailing window size in days (default: %(default)s)")
    p_stats.add_argument(
        "--home", default=None,
        help="base dir holding hook.log (default: $MEMCONTINUUM_HOME or ~/.memcontinuum)",
    )
    p_stats.add_argument(
        "--store", default=None,
        help="store markdown root's git repo -- counts real, store-WIDE commits in "
             "the window (git log --since/--until, bounded on both ends) as "
             "CORROBORATION ONLY; never gates either FLAG (a store can be shared by "
             "more than one project, and one unrelated commit must not hide a "
             "genuinely silent project) -- the write-side FLAG is driven entirely by "
             "this project's own store-kind ledger appends, with or without --store",
    )
    p_stats.add_argument("--json", action="store_true", help="machine-readable output instead of plain text")
    p_stats.add_argument("--now", default=None, help=argparse.SUPPRESS)
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
