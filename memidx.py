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
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

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
        ruling_text_parts = []
        for link in links:
            ruling = link.get("ruling") or {}
            rationale = link.get("rationale") or {}
            if ruling.get("text"):
                ruling_text_parts.append(str(ruling["text"]))
            if rationale.get("text"):
                ruling_text_parts.append(str(rationale["text"]))
        ruling_text = "\n".join(ruling_text_parts)
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


def open_db(db_path: Path, project: str | None = None) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    ensure_links_invariant_column(conn)
    if project is not None:
        enforce_project_isolation(conn, db_path, project)
    return conn


def resolve_db_path(args) -> Path:
    if getattr(args, "db", None):
        return Path(args.db).expanduser().resolve()
    base = Path(os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum")))
    return base / f"{args.project}.sqlite"


def delete_record_rows(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM records WHERE path=?", (path,))
    conn.execute("DELETE FROM links WHERE topic_path=?", (path,))
    conn.execute("DELETE FROM fts WHERE path=?", (path,))
    conn.execute("DELETE FROM embeddings WHERE path=?", (path,))
    conn.execute("DELETE FROM edges WHERE topic_path=?", (path,))
    conn.execute("DELETE FROM assumptions WHERE topic_path=?", (path,))
    conn.execute("DELETE FROM concepts WHERE path=?", (path,))
    conn.execute("DELETE FROM concept_paths WHERE source_path=?", (path,))


def insert_record_rows(conn: sqlite3.Connection, project: str, rec: dict, sha: str, mtime: float, size: int) -> None:
    conn.execute(
        """INSERT INTO records
           (path, sha256, mtime, size, project, type, id, title, area, topic,
            status, authority, tags, code_refs, body, ruling_text)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            rec["path"], sha, mtime, size, project, rec["type"], rec["id"], rec["title"],
            rec["area"], rec["topic"], rec["status"], rec["authority"], rec["tags"],
            rec["code_refs"], rec["body"], rec["ruling_text"],
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
                    recorded_by, recorded_at, invariant)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec["path"], rec["id"], project, link.get("link"), seq,
                    str(link.get("date") or ""), link.get("status"), link.get("kind"),
                    link.get("reverses"), link.get("reason_for_change"),
                    ruling.get("text"), ruling.get("authority"), ruling.get("source"),
                    rationale.get("text"), rationale.get("authority"),
                    link.get("superseded_by"), json.dumps(link.get("revisit_if") or []),
                    link.get("recorded_by"), str(link.get("recorded_at") or ""),
                    json.dumps(invariant) if invariant else None,
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
    root = Path(args.root).resolve()
    db_path = resolve_db_path(args)
    conn = open_db(db_path, project=args.project)
    t0 = time.time()

    existing = {
        row["path"]: (row["sha256"], row["mtime"])
        for row in conn.execute("SELECT path, sha256, mtime FROM records WHERE project=?", (args.project,))
    }

    files = sorted(walk_markdown(root))
    seen = set()
    to_embed_paths: list[str] = []
    to_embed_texts: list[str] = []
    pending: list[tuple[dict, str, float, int]] = []
    unchanged = 0

    for f in files:
        path_str = str(f)
        seen.add(path_str)
        stat = f.stat()
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        prev = existing.get(path_str)
        if prev and not args.full and prev[0] == sha:
            unchanged += 1
            continue
        fm, body = parse_frontmatter(f)
        rec = build_record(root, f, fm, body)
        rec["project"] = args.project
        pending.append((rec, sha, stat.st_mtime, stat.st_size))
        if not args.no_embed:
            to_embed_paths.append(path_str)
            to_embed_texts.append(embed_text_for(rec))

    vectors_by_path: dict[str, bytes] = {}
    if to_embed_texts:
        vecs = compute_embeddings(to_embed_texts)
        for p, v in zip(to_embed_paths, vecs):
            vectors_by_path[p] = pack_vector(v)

    added = 0
    changed = 0
    for rec, sha, mtime, size in pending:
        is_new = rec["path"] not in existing
        delete_record_rows(conn, rec["path"])
        insert_record_rows(conn, args.project, rec, sha, mtime, size)
        if rec["path"] in vectors_by_path:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings (path, project, dim, vector) VALUES (?,?,?,?)",
                (rec["path"], args.project, len(unpack_vector(vectors_by_path[rec["path"]])), vectors_by_path[rec["path"]]),
            )
        if is_new:
            added += 1
        else:
            changed += 1

    removed_paths = set(existing.keys()) - seen
    for p in removed_paths:
        delete_record_rows(conn, p)

    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    print(
        f"reindex: {len(files)} files scanned, {added} added, {changed} changed, "
        f"{unchanged} unchanged, {len(removed_paths)} removed, {elapsed:.3f}s"
    )
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


def build_filter_clause(args) -> tuple[str, list]:
    clauses = ["project=?"]
    params: list = [args.project]
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
    body = row["body"] or ""
    return body.strip().replace("\n", " ")[:200]


def fts_ranked(conn, query: str, project: str) -> list[str]:
    q = fts_escape(query)
    rows = conn.execute(
        "SELECT path FROM fts WHERE fts MATCH ? AND project=? ORDER BY bm25(fts) LIMIT 200",
        (q, project),
    ).fetchall()
    return [r["path"] for r in rows]


def fts_escape(query: str) -> str:
    terms = [t for t in query.replace('"', " ").split() if t]
    return " OR ".join(f'"{t}"' for t in terms) if terms else '""'


def vector_ranked(conn, query: str, project: str) -> list[tuple[str, float]]:
    qvec = compute_query_embedding(query)
    rows = conn.execute("SELECT path, vector FROM embeddings WHERE project=?", (project,)).fetchall()
    scored = [(r["path"], cosine(qvec, unpack_vector(r["vector"]))) for r in rows]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def cmd_search(args) -> int:
    db_path = resolve_db_path(args)
    conn = open_db(db_path, project=args.project)
    allowed = filtered_paths(conn, args)

    results: list[tuple[str, float]] = []
    if args.mode == "fts":
        ranked = fts_ranked(conn, args.query, args.project)
        results = [(p, float(len(ranked) - i)) for i, p in enumerate(ranked) if p in allowed]
    elif args.mode == "vector":
        ranked = vector_ranked(conn, args.query, args.project)
        results = [(p, s) for p, s in ranked if p in allowed]
    elif args.mode == "hybrid":
        fts_list = [p for p in fts_ranked(conn, args.query, args.project) if p in allowed]
        vec_list = [p for p, _ in vector_ranked(conn, args.query, args.project) if p in allowed]
        k = 60
        scores: dict[str, float] = {}
        for i, p in enumerate(fts_list):
            scores[p] = scores.get(p, 0.0) + 1.0 / (k + i + 1)
        for i, p in enumerate(vec_list):
            scores[p] = scores.get(p, 0.0) + 1.0 / (k + i + 1)
        results = sorted(scores.items(), key=lambda t: t[1], reverse=True)
    else:
        raise ValueError(f"unknown mode {args.mode}")

    results = results[: args.limit]
    out = []
    for path, score in results:
        row = record_row_by_path(conn, path)
        if row is None:
            continue
        out.append(
            {
                "path": row["path"],
                "id": row["id"],
                "title": row["title"],
                "type": row["type"],
                "status": row["status"],
                "area": row["area"],
                "topic": row["topic"],
                "score": score,
                "snippet": snippet_for(row),
            }
        )

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
    conn = open_db(db_path, project=args.project)
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


def code_ref_matches(file_path: str, code_ref: str) -> bool:
    ref_path = code_ref.split("#", 1)[0]
    if file_path == ref_path:
        return True
    if file_path.startswith(ref_path) or ref_path.startswith(file_path):
        return True
    if fnmatch.fnmatch(file_path, ref_path):
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


def fragment_declared_in_text(frag: str, text: str, rel_path: str = "x.swift") -> bool:
    """memlint's #symbol vocabulary check (memlint.py's lint_concept): is
    `frag` a symbol actually DECLARED in `text`?

    Dispatch is fully generic (Anatomy M1 fix wave, I3): the file's
    language comes from chunkers.lang_for_path(rel_path), and the answer
    comes from that backend's own `declared_symbols(text)` -- a uniform
    part of the registry contract, alongside `chunk_file`. There is no
    per-language branch here and no Swift lexer call: adding a third
    language means adding a LANGUAGE_TABLE row with a backend that exposes
    declared_symbols, and this check follows for free. A rel_path with no
    registered language falls back to the swift backend, which is what the
    default "x.swift" preserves for every pre-existing call site
    (resolve_symbol_to_path's bare-symbol fallback among them).

    Each backend returns `(symbol, qualified_name)` pairs -- including its
    container type names (Swift's class/struct/enum/protocol/extension/
    actor, Python's classes), since a #symbol fragment may name the type
    itself rather than a member. The pairs are evaluated with the SAME
    fragment_matches_symbol predicate code-search's per-hit concept
    attachment uses, so a fragment written qualified (e.g.
    "Outer.outerFunc") validates identically on both surfaces."""
    lang = chunkers.lang_for_path(rel_path) or "swift"
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
    db_path = resolve_db_path(args)
    conn = open_db(db_path, project=args.project)
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


# ---------------------------------------------------------------------------
# code-tree helpers shared by `why` (symbol resolution) and `drift` (invariant checks)
# ---------------------------------------------------------------------------

SKIP_DIR_NAMES = {".git", ".build"}


def is_binary_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    return b"\0" in chunk


def iter_code_files(code_root: Path):
    for dirpath, dirnames, filenames in os.walk(code_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            if is_binary_file(fpath):
                continue
            yield fpath


def _resolve_symbol_via_code_index(code_root: Path, symbol: str, project: str) -> str | None:
    """Finding 7 fast path for resolve_symbol_to_path, below: consult the
    code index's `chunks` table (member symbols only -- container names
    like class/struct/enum/protocol/extension/actor are never chunks
    themselves, see chunk_source's own docstring -- so a miss here is
    never conclusive; the caller always falls back to the full lexer
    scan, which does see container names too). Deliberately never
    consults `--db`: on `why`, that flag means the DECISION db override
    (exactly the confusion --decision-db exists to prevent for
    code-search's own concept attachment) -- only the default
    "<project>-code.sqlite" path is ever read here. Only trusted when the
    index was built against this SAME code_root -- code_meta.code_root
    mismatch (a stale index built against a different tree, or one that
    predates this code_root entirely) is treated exactly like no index at
    all, since its stored relative chunk paths would otherwise resolve
    against the wrong root."""
    code_db_path = (
        Path(os.environ.get("MEMCONTINUUM_HOME", str(Path.home() / ".memcontinuum")))
        / f"{project}-code.sqlite"
    )
    if not code_db_path.exists():
        return None
    try:
        conn = open_code_db(code_db_path)
    except sqlite3.DatabaseError:
        return None
    try:
        meta = conn.execute(
            "SELECT code_root FROM code_meta WHERE project=?", (project,)
        ).fetchone()
        if not meta or not meta["code_root"]:
            return None
        try:
            if Path(meta["code_root"]).resolve() != code_root.resolve():
                return None
        except OSError:
            return None
        rows = conn.execute(
            "SELECT path, symbol, qualified_name FROM chunks WHERE project=? ORDER BY path",
            (project,),
        ).fetchall()
        for r in rows:
            if fragment_matches_symbol(symbol, r["symbol"], r["qualified_name"]):
                return r["path"]
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
    `project` is given, for speed; always falls back to scanning
    code_root directly (same SKIP_DIR_NAMES as before -- deliberately NOT
    the broader CODE_SKIP_DIR_NAMES the code index itself uses, since
    `why` must stay able to resolve a symbol declared under Tests/, a
    behavior change nobody asked for) so a missing/stale/member-only-index
    miss never regresses a resolution the old regex-based version could
    already make."""
    if project:
        resolved = _resolve_symbol_via_code_index(code_root, symbol, project)
        if resolved is not None:
            return resolved
    for fpath in sorted(iter_code_files(code_root)):
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if fragment_declared_in_text(symbol, text):
            try:
                return str(fpath.relative_to(code_root))
            except ValueError:
                return str(fpath)
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
    conn = open_db(db_path, project=args.project)
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


def links_with_active_invariant(conn, project: str):
    """(link row, invariant dict) for every active link that carries one."""
    rows = conn.execute(
        "SELECT * FROM links WHERE project=? AND status='active' AND invariant IS NOT NULL",
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
    drift evidence lines -- empty means the invariant holds."""
    kind = invariant.get("kind")
    pattern = re.compile(invariant.get("pattern") or "")
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
        return [] if len(all_hits) == 1 else all_hits

    if kind == "must-call":
        scope = invariant.get("scope")
        if not scope:
            return []
        missing = []
        for fpath in sorted(code_root.glob(scope)):
            if not fpath.is_file() or is_binary_file(fpath):
                continue
            rel = str(fpath.relative_to(code_root))
            text = fpath.read_text(encoding="utf-8", errors="ignore")
            if not pattern.search(text):
                missing.append(rel)
        return missing

    return []


def cmd_drift(args) -> int:
    db_path = resolve_db_path(args)
    conn = open_db(db_path, project=args.project)
    code_root = Path(args.code_root).resolve()
    entries = links_with_active_invariant(conn, args.project)
    conn.close()

    results = []
    for link_row, invariant in entries:
        hits = check_invariant(code_root, invariant)
        if hits:
            results.append(
                {
                    "topic": link_row["topic_id"],
                    "link": link_row["link"],
                    "kind": invariant.get("kind"),
                    "hits": hits,
                }
            )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("drift: no invariants violated")
        for r in results:
            label = f"{r['topic']}/{r['link']}"
            joined = ", ".join(r["hits"])
            if r["kind"] in ("pattern-absent", "no-bypass"):
                print(f"DRIFT: {label} — {len(r['hits'])} hits outside allowed: {joined}")
            elif r["kind"] == "single-definition":
                print(f"DRIFT: {label} — expected exactly 1 definition, found {len(r['hits'])}: {joined}")
            elif r["kind"] == "must-call":
                print(f"DRIFT: {label} — {len(r['hits'])} file(s) missing required call: {joined}")
            else:
                print(f"DRIFT: {label} — {len(r['hits'])} hits: {joined}")
    return 1 if results else 0


# ---------------------------------------------------------------------------
# unmapped -- write-side reminder hooks' engine addition (docs/DESIGN.md)
# ---------------------------------------------------------------------------


def _index_has_drift(conn: sqlite3.Connection, root: Path, project: str) -> bool:
    """Same added/changed/removed-by-mtime-and-size comparison as cmd_check,
    factored out so `unmapped` can reuse it without cmd_check's printing."""
    existing = {
        row["path"]: (row["mtime"], row["size"])
        for row in conn.execute("SELECT path, mtime, size FROM records WHERE project=?", (project,))
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

    Self-healing: runs the same added/changed/removed drift check `check`
    uses; if the store markdown under --root has drifted since the last
    reindex, runs `reindex --no-embed` once and re-checks. If drift still
    can't be resolved (a genuine failure -- corrupt db, unreadable root,
    etc.) coverage_status is "unknown" and `unmapped` is always [] (a
    positive match found on a not-fully-current index is still real
    evidence; the *absence* of a match is what an unknown-freshness index
    must never be allowed to assert -- docs/DESIGN.md ruling F,
    "never a false gap").
    """
    root = Path(args.root).resolve()
    db_path = resolve_db_path(args)
    code_root = Path(args.code_root).resolve() if getattr(args, "code_root", None) else None

    coverage_status = "ok"
    conn: sqlite3.Connection | None = None
    try:
        conn = open_db(db_path, project=args.project)
        if _index_has_drift(conn, root, args.project):
            reindex_ns = argparse.Namespace(
                root=str(root), project=args.project, db=str(db_path), full=False, no_embed=True
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_reindex(reindex_ns)
            conn.close()
            conn = open_db(db_path, project=args.project)
            if _index_has_drift(conn, root, args.project):
                coverage_status = "unknown"
    except Exception:
        coverage_status = "unknown"
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    mapped_topic: list[str] = []
    mapped_concept_only: list[str] = []
    unmapped: list[str] = []

    if conn is not None:
        for raw_path in args.paths:
            candidates = _unmapped_path_candidates(raw_path, code_root)
            display = _unmapped_display_path(raw_path, code_root)
            topic_hit = any(topic_matches_for_path(conn, args.project, c) for c in candidates)
            concept_hit = False
            if not topic_hit:
                concept_hit = any(concept_matches_for_path(conn, args.project, c) for c in candidates)
            if topic_hit:
                mapped_topic.append(display)
            elif concept_hit:
                mapped_concept_only.append(display)
            elif coverage_status == "ok":
                unmapped.append(display)
        conn.close()

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
    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def cmd_check(args) -> int:
    root = Path(args.root).resolve()
    db_path = resolve_db_path(args)
    conn = open_db(db_path, project=args.project)
    existing = {
        row["path"]: (row["mtime"], row["size"])
        for row in conn.execute("SELECT path, mtime, size FROM records WHERE project=?", (args.project,))
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

# Task 7: this is now the GLOBAL skip set ONLY -- directory names that are
# always noise regardless of which languages are wired. Language-specific
# noise (swift's Tests/Resources/.build, python's venv/.venv/__pycache__/
# build/dist/.tox/.eggs) lives on each LANGUAGE_TABLE row's "skip_dirs" key
# instead, so a Swift-only project's own build/ or dist/ output is never
# pruned by a rule meant for Python virtualenvs, and vice versa. This
# global set is what iter_code_source_files prunes for every walk;
# per-language sets are applied per FILE, to that language's own files
# only (fix wave C1 -- see chunkers.common_skip_dirs).
CODE_SKIP_DIR_NAMES = {".git", "vendor", "node_modules"}

CODE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  project TEXT NOT NULL,
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

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  qualified_name, split_tokens, signature, doc, body
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
  sha256 TEXT NOT NULL,
  mtime REAL,
  size INTEGER,
  gap_count INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (path, project)
);
-- chunker_version (TASK 3, joins the skip decision alongside sha256) is
-- NOT listed above -- same reasoning as code_meta.head_sha below:
-- CREATE TABLE IF NOT EXISTS never adds a column to a table that already
-- exists on disk, so a brand-new DB relies on the unconditional migration
-- guard just like an upgraded one does (ensure_file_sha_chunker_version_column,
-- called from open_code_db right after this script runs).

CREATE TABLE IF NOT EXISTS code_meta (
  project TEXT PRIMARY KEY,
  code_root TEXT,
  langs TEXT,
  last_indexed_at REAL,
  head_sha TEXT
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


def ensure_code_meta_head_sha_column(conn: sqlite3.Connection) -> None:
    """Migration guard (finding 1, index provenance): a code_meta table
    created by pre-provenance memidx.py has no head_sha column --
    CREATE TABLE IF NOT EXISTS never adds columns to an existing table
    (mirrors ensure_links_invariant_column's same fix for `links`)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(code_meta)").fetchall()}
    if "head_sha" not in cols:
        conn.execute("ALTER TABLE code_meta ADD COLUMN head_sha TEXT")


def ensure_file_sha_chunker_version_column(conn: sqlite3.Connection) -> None:
    """Migration guard (Task 3, Anatomy M1): a file_sha table created
    before chunker_version joined the skip decision has no such column --
    CREATE TABLE IF NOT EXISTS never adds columns to an existing table
    (same fix as ensure_code_meta_head_sha_column above). NULL on an
    upgraded row (and on any row this migration adds the column for) is
    the documented pre-stamp value: the skip check below never treats
    NULL as equal to a real chunker_version string, so every pre-existing
    row re-chunks exactly once and gets stamped."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(file_sha)").fetchall()}
    if "chunker_version" not in cols:
        conn.execute("ALTER TABLE file_sha ADD COLUMN chunker_version TEXT")


def open_code_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(CODE_SCHEMA_SQL)
    ensure_code_meta_head_sha_column(conn)
    ensure_file_sha_chunker_version_column(conn)
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

    Used by all three places that used to answer this question separately:
    the walk's classification, cmd_code_reindex's per-file dispatch (which
    used to call the extension-only `_lang_for_ext`) and the staleness
    check's per-file chunker-version comparison."""
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


def iter_code_source_files(root: Path, langs: list[str] | None, skipped: Counter | None = None):
    """Walk `root`, yielding source files whose language (see
    `lang_for_source_file` -- extension, or a shebang on an extensionless
    file) is in the wired set (`langs`). Dispatch is registry-driven
    rather than the old locally duplicated extension map.

    `langs` defaults to swift-only when falsy -- a legacy-row safety net for
    _code_index_is_stale below, whose only caller reads it out of an
    existing code_meta.langs column that has been non-empty on every row
    ever written since langs became mandatory (Task 7); it does not mean
    code-reindex itself still has a default (it doesn't -- see
    cmd_code_reindex's --lang resolution, which fails outright rather than
    reaching this fallback).

    Directory pruning (fix wave C1, superseding Task 7's union rule): a
    language's skip_dirs prune only THAT language's own files. The walk
    prunes CODE_SKIP_DIR_NAMES (global noise: .git, vendor, node_modules)
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
    existing callers (_code_index_is_stale) that iterate this generator
    for plain paths need no change."""
    wired = list(langs or ["swift"])
    wired_set = set(wired)
    skip_dirs = CODE_SKIP_DIR_NAMES | chunkers.common_skip_dirs(wired)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in sorted(filenames):
            path = Path(dirpath) / fname
            lang = lang_for_source_file(path)
            if lang is not None and lang in wired_set:
                try:
                    rel_parts = path.relative_to(root).parts
                except ValueError:
                    rel_parts = (fname,)
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


def delete_code_chunks_for_path(conn: sqlite3.Connection, project: str, path: str) -> None:
    rows = conn.execute("SELECT id FROM chunks WHERE project=? AND path=?", (project, path)).fetchall()
    for r in rows:
        conn.execute("DELETE FROM fts WHERE rowid=?", (r["id"],))
        conn.execute("DELETE FROM embeddings WHERE chunk_id=?", (r["id"],))
    conn.execute("DELETE FROM chunks WHERE project=? AND path=?", (project, path))


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


def cmd_code_reindex(args) -> int:
    root = Path(args.code_root).resolve()
    db_path = resolve_code_db_path(args)
    conn = open_code_db(db_path)
    t0 = time.time()

    # Task 7: the old hardcoded "swift" --lang default is gone. Omitted
    # --lang reuses code_meta.langs from a PRIOR reindex of this project,
    # when one is stored; a project with no stored langs yet (its first
    # reindex) must name its languages explicitly -- silently defaulting to
    # swift-only used to index nothing at all for a python-only project set
    # up without --lang, and never say why.
    if getattr(args, "lang", None):
        langs = [l.strip() for l in args.lang.split(",")]
    else:
        meta_row = conn.execute(
            "SELECT langs FROM code_meta WHERE project=?", (args.project,)
        ).fetchone()
        stored = meta_row["langs"] if meta_row else None
        if stored:
            langs = [l.strip() for l in stored.split(",")]
        else:
            print(
                f"code-reindex: --lang required on first code-reindex for a project "
                f"(no stored langs yet for {args.project!r})",
                file=sys.stderr,
            )
            conn.close()
            return 1

    # C2 (Anatomy M1 fix wave, Codex): every RESOLVED language name --
    # whether it came from --lang or from a code_meta row a previous run
    # stored -- must name a real LANGUAGE_TABLE row. A typo used to be
    # tolerated silently: the walk matched zero files for it, the run said
    # nothing, and the bad name was then PERSISTED to code_meta.langs, so
    # every later run reused it. That is exactly the silent blind spot
    # this milestone exists to close, so it is now a loud failure naming
    # the languages this engine actually knows, before anything is walked
    # or written.
    unknown = [l for l in langs if l not in chunkers.LANGUAGE_TABLE]
    if unknown:
        print(
            f"code-reindex: unknown language(s): {', '.join(unknown)} -- "
            f"this engine version knows: {', '.join(sorted(chunkers.LANGUAGE_TABLE))}",
            file=sys.stderr,
        )
        conn.close()
        return 1

    existing = {
        row["path"]: (row["sha256"], row["chunker_version"])
        for row in conn.execute(
            "SELECT path, sha256, chunker_version FROM file_sha WHERE project=?", (args.project,)
        )
    }

    skipped_unknown: Counter = Counter()
    files = list(iter_code_source_files(root, langs, skipped_unknown))
    seen = set()
    added_files = changed_files = unchanged_files = failed_files = 0
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
    #     keeps _code_index_state honest: the file is on disk, absent from
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
        try:
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            lang = lang_for_source_file(f)
            try:
                cv = chunkers.chunker_version(lang)
            except KeyError:
                # Task 3 edge case: a lang with no LANGUAGE_TABLE row must
                # not crash code-reindex -- fail open with a literal stamp
                # instead of raising. (Unreachable via --lang since C2
                # validates the resolved language set; kept as a guard.)
                cv = "unversioned"
            prev_sha, prev_cv = existing.get(rel, (None, None))
            if prev_sha == sha and prev_cv == cv and not args.full:
                unchanged_files += 1
                # Finding 2 (staleness): the file's content (and thus its
                # chunks) didn't change, but its mtime/size on disk may
                # have (e.g. a bare `touch`) -- refresh the stored
                # file_sha row's mtime/size so _code_index_is_stale's
                # on-disk comparison matches again. sha256/gap_count/
                # chunker_version are untouched (nothing about the indexed
                # content or the chunker that produced it changed), so
                # this is a plain UPDATE, not the INSERT OR REPLACE the
                # changed/added branch below uses.
                stat = f.stat()
                conn.execute(
                    "UPDATE file_sha SET mtime=?, size=? WHERE project=? AND path=?",
                    (stat.st_mtime, stat.st_size, args.project, rel),
                )
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

            delete_code_chunks_for_path(conn, args.project, rel)
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
                    """INSERT INTO chunks (path, project, lang, kind, symbol, qualified_name,
                           signature, doc, start_line, end_line)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rel, args.project, chunk["lang"], chunk["kind"], chunk["symbol"],
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
            conn.execute(
                "INSERT OR REPLACE INTO file_sha (path, project, sha256, mtime, size, gap_count, chunker_version) "
                "VALUES (?,?,?,?,?,?,?)",
                (rel, args.project, sha, stat.st_mtime, stat.st_size, len(gaps), cv),
            )
            pending_texts.extend(file_texts)
            pending_ids.extend(file_ids)
            if is_new:
                added_files += 1
            else:
                changed_files += 1
        except Exception as exc:
            # B1: purge whatever this path still has in the index, so no
            # stale row outlives the source that produced it, and the
            # missing file_sha row keeps the index reading "stale".
            try:
                delete_code_chunks_for_path(conn, args.project, rel)
                conn.execute(
                    "DELETE FROM file_sha WHERE project=? AND path=?", (args.project, rel)
                )
            except Exception:
                pass
            failed_files += 1
            print(
                f"code-reindex: WARNING {rel} not indexed: "
                f"{type(exc).__name__}: {exc} -- any previously indexed chunks for "
                "this file were removed (repair will retry)",
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
        delete_code_chunks_for_path(conn, args.project, rel)
        conn.execute("DELETE FROM file_sha WHERE project=? AND path=?", (args.project, rel))

    # Task 7: the "swift" fallback here is gone -- `langs` is always a
    # non-empty, resolved list by this point (explicit --lang, or reused
    # code_meta.langs; the no-langs-yet case already returned 1 above), so
    # a fallback here would just be dead code hiding a real bug if one of
    # those guarantees ever broke.
    conn.execute(
        "INSERT OR REPLACE INTO code_meta (project, code_root, langs, last_indexed_at, head_sha) "
        "VALUES (?,?,?,?,?)",
        (args.project, str(root), ",".join(langs), time.time(), _git_head_sha(root)),
    )
    conn.commit()
    conn.close()
    elapsed = time.time() - t0
    print(
        f"code-reindex: {len(files)} files scanned, {added_files} added, {changed_files} changed, "
        f"{unchanged_files} unchanged, {len(removed)} removed, {failed_files} failed, "
        f"{reembeds} chunk(s) (re-)embedded, {total_gaps} gap(s) warned, {elapsed:.3f}s"
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


def _census_skip_dirs() -> set:
    """Directory names pruned from a code-census walk: CODE_SKIP_DIR_NAMES
    (the global set: .git/vendor/node_modules) unioned with EVERY
    LANGUAGE_TABLE row's skip_dirs.

    Census runs BEFORE any language is wired -- it is the discovery step
    repo-init's consent dialogue reads -- so there is no wired subset to
    reason about, and the walk has to already look clean before the user
    has chosen anything. On the global set alone, an untouched Python
    project's census would count thousands of files under .venv/ as
    signal.

    Contrast iter_code_source_files, which answers a different question
    once a language set exists: it prunes the global set plus the
    INTERSECTION of the wired languages' skip sets, and drops a file only
    when an ancestor directory is in ITS OWN language's set.
    """
    dirs = set(CODE_SKIP_DIR_NAMES)
    for row in chunkers.LANGUAGE_TABLE.values():
        dirs.update(row.get("skip_dirs", ()))
    return dirs


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
    a LANGUAGE_TABLE lang, whether by extension via chunkers.lang_for_path
    or -- for an extensionless executable -- by chunkers.lang_for_shebang
    matching a shebang stem) or UNSUPPORTED (an extension with no
    LANGUAGE_TABLE row, or an extensionless file whose first line is not a
    recognized shebang, bucketed under NO_EXTENSION_BUCKET). This is the
    "three ways" the brief names: extension-supported, extension-
    unsupported, and shebang-sniffed extensionless (itself supported or
    unsupported depending on whether the shebang matched) -- the shebang
    path folds into the SAME lang key an extension match would use, not a
    separate status, so a `#!/usr/bin/env python3` script and a `foo.py`
    file both count under the "python" key.

    Returns {key: {"files": n, "status": "supported"|"unsupported"}} (the
    brief's JSON shape) -- `key` is a lang name for a supported row, else
    the raw extension string (or NO_EXTENSION_BUCKET) for an unsupported
    one. Directory pruning: _census_skip_dirs() (global set UNION every
    LANGUAGE_TABLE row's skip_dirs -- see its docstring for why this is
    wider than a wired-langs walk). Fails open per file and never raises on
    a walk it can complete: os.walk over a missing/unreadable root just
    yields nothing, so an empty or nonexistent root produces an empty dict,
    not an error."""
    skip_dirs = _census_skip_dirs()
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
            # I2: compound-extension aware, so `foo.blade.php` is keyed
            # ".blade.php" and never folded into the ".php" bucket
            # lang_for_path already refuses to treat it as.
            ext = chunkers.extension_of(fname)
            if ext:
                lang = chunkers.lang_for_path(path)
                if lang is not None:
                    bump(lang, "supported")
                else:
                    bump(ext, "unsupported")
                continue
            # Extensionless: only a recognized shebang saves it from the
            # catch-all bucket (controller-scope addition, Task 5 reviewer
            # finding -- the "second silent gap": extensionless scripts
            # used to vanish from the census entirely).
            first_line = _first_line_or_none(path)
            lang = chunkers.lang_for_shebang(first_line) if first_line else None
            if lang is not None:
                bump(lang, "supported")
            else:
                bump(NO_EXTENSION_BUCKET, "unsupported")

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
    q = fts_escape(query)
    rows = conn.execute(
        """SELECT fts.rowid AS rowid FROM fts
           JOIN chunks ON chunks.id = fts.rowid
           WHERE fts MATCH ? AND chunks.project=?
           ORDER BY bm25(fts) LIMIT ?""",
        (q, project, limit),
    ).fetchall()
    return [r["rowid"] for r in rows]


def code_hits_vector(conn: sqlite3.Connection, query: str, project: str):
    qvec = compute_query_embedding(query)
    rows = conn.execute(
        "SELECT chunk_id, vector FROM embeddings WHERE project=?", (project,)
    ).fetchall()
    scored = [(r["chunk_id"], cosine(qvec, unpack_vector(r["vector"]))) for r in rows]
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored


def _code_index_is_stale(conn: sqlite3.Connection, project: str) -> bool:
    """Stale when the tree and the stored rows disagree in ANY of three
    ways: a file's mtime/size drifted, a file is on disk with no stored
    row at all, or a stored row was produced by a DIFFERENT chunker
    version than the one this engine would use today (B2, Anatomy M1 fix
    wave). Plus the missing-code_root case.

    The chunker-version comparison is the one this check used to be
    missing. `code-reindex` has always re-chunked a file whose stored
    chunker_version no longer matches, but `code-search`'s staleness
    report only compared mtime/size -- so bumping a chunker's
    impl_version left every stored row reading "current" until something
    on disk happened to change, which is precisely when a reader most
    needs to be told the index predates the current chunker. A row for a
    language with no LANGUAGE_TABLE row compares against "unversioned",
    the same literal cmd_code_reindex's own fail-open stamps."""
    meta = conn.execute("SELECT code_root, langs FROM code_meta WHERE project=?", (project,)).fetchone()
    if meta is None or not meta["code_root"]:
        return False
    root = Path(meta["code_root"])
    if not root.exists():
        return True
    langs = meta["langs"].split(",") if meta["langs"] else None
    existing = {
        row["path"]: (row["mtime"], row["size"], row["chunker_version"])
        for row in conn.execute(
            "SELECT path, mtime, size, chunker_version FROM file_sha WHERE project=?", (project,)
        )
    }
    seen = set()
    for f in iter_code_source_files(root, langs):
        try:
            rel = str(f.relative_to(root))
        except ValueError:
            continue
        seen.add(rel)
        stat = f.stat()
        prev = existing.get(rel)
        if prev is None or prev[0] != stat.st_mtime or prev[1] != stat.st_size:
            return True
        try:
            cv = chunkers.chunker_version(lang_for_source_file(f))
        except KeyError:
            cv = "unversioned"
        if prev[2] != cv:
            return True
    return bool(set(existing.keys()) - seen)


def _code_index_state(conn: sqlite3.Connection, project: str):
    """Finding 1 (HIGH, index provenance): the three states code-search
    must distinguish and SAY, not collapse into a bare empty result --
    "uninitialized" (`code-reindex` was never run for this project: no
    code_meta row, or one with no code_root ever recorded -- the
    never-indexed-reads-as-healthy-empty bug), "stale" (existing
    _code_index_is_stale mtime/size-drift or missing-code_root-dir check),
    "current" (neither). Returns (state, meta_row_or_None)."""
    meta = conn.execute(
        "SELECT code_root, langs, last_indexed_at, head_sha FROM code_meta WHERE project=?",
        (project,),
    ).fetchone()
    if meta is None or not meta["code_root"]:
        return "uninitialized", None
    if _code_index_is_stale(conn, project):
        return "stale", meta
    return "current", meta


def cmd_code_search(args) -> int:
    db_path = resolve_code_db_path(args)
    conn = open_code_db(db_path)

    state, meta = _code_index_state(conn, args.project)

    if state == "uninitialized":
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

    if state == "stale":
        print(
            "code-search: WARNING code index appears stale "
            "(source changed since last code-reindex)",
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
    md_conn = None
    if md_db_path.exists():
        try:
            md_conn = open_db(md_db_path, project=args.project)
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
        hit = {
            "path": row["path"],
            "line": row["start_line"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "signature": row["signature"],
            "score": score,
        }
        if md_conn is not None:
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
        # Finding 1: state + index provenance (code_root/indexed_at/
        # head_sha, stored at reindex time) surface here alongside
        # `results` -- an envelope, not a bare list, so a caller can tell
        # "current" apart from "stale" apart from an empty-but-healthy
        # result without a separate call.
        print(json.dumps(
            {
                "state": state,
                "code_root": meta["code_root"],
                "indexed_at": meta["last_indexed_at"],
                "head_sha": meta["head_sha"],
                "results": out,
            },
            indent=2,
        ))
    else:
        for h in out:
            extra = f"  [{h['concept_id']}]" if "concept_id" in h else ""
            print(f"{h['score']:.4f}  {h['path']}:{h['line']}  {h['qualified_name']}  {h['signature']}{extra}")
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
    p_drift.set_defaults(func=cmd_drift)

    p_unmapped = sub.add_parser("unmapped")
    add_common_args(p_unmapped, need_root=True)
    p_unmapped.add_argument("paths", nargs="+", metavar="PATH")
    p_unmapped.add_argument("--code-root", dest="code_root", default=None)
    p_unmapped.add_argument("--json", action="store_true")
    p_unmapped.set_defaults(func=cmd_unmapped)

    p_code_reindex = sub.add_parser("code-reindex")
    add_common_args(p_code_reindex)
    p_code_reindex.add_argument("--code-root", dest="code_root", required=True)
    p_code_reindex.add_argument(
        "--lang", default=None,
        help="comma-separated language filter, e.g. swift,python. Required on a "
             "project's first code-reindex; omit it on later runs to reuse the "
             "langs stored from the first run.",
    )
    p_code_reindex.add_argument("--no-embed", action="store_true")
    p_code_reindex.add_argument("--full", action="store_true")
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
    p_code_search.set_defaults(func=cmd_code_search)

    p_code_census = sub.add_parser("code-census")
    p_code_census.add_argument("--root", required=True)
    p_code_census.add_argument("--json", action="store_true")
    p_code_census.set_defaults(func=cmd_code_census)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
