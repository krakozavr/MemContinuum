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
import sys
import time
from pathlib import Path

import yaml

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


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    ensure_links_invariant_column(conn)
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


def walk_markdown(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
        for fname in filenames:
            if fname.endswith(".md"):
                yield Path(dirpath) / fname


def cmd_reindex(args) -> int:
    root = Path(args.root).resolve()
    db_path = resolve_db_path(args)
    conn = open_db(db_path)
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
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


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
    conn = open_db(db_path)
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
    conn = open_db(db_path)
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
    conn = open_db(db_path)
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
SYMBOL_DEF_RE = r"\b(?:func|class|struct|enum|let|var)\s+{sym}\b"


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


def resolve_symbol_to_path(code_root: Path, symbol: str) -> str | None:
    """grep code_root for a `func|class|struct|enum|let|var <symbol>` definition;
    return the first matching file's path relative to code_root, or None."""
    pattern = re.compile(SYMBOL_DEF_RE.format(sym=re.escape(symbol)))
    for fpath in sorted(iter_code_files(code_root)):
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
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
        resolved = resolve_symbol_to_path(Path(args.code_root).resolve(), target)
        if resolved is None:
            print(f"why: no definition of {target!r} found under {args.code_root}", file=sys.stderr)
            return 1
        file_path = resolved

    db_path = resolve_db_path(args)
    conn = open_db(db_path)
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
    conn = open_db(db_path)
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
        conn = open_db(db_path)
        if _index_has_drift(conn, root, args.project):
            reindex_ns = argparse.Namespace(
                root=str(root), project=args.project, db=str(db_path), full=False, no_embed=True
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_reindex(reindex_ns)
            conn.close()
            conn = open_db(db_path)
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
    conn = open_db(db_path)
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
