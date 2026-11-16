"""Meaning-based search, to sit next to keyword search.

BM25 only finds a passage when the question and the speech share words. "A
feature to stop the remote getting lost" shares none with "it beeps when you
whistle". An embedding model puts both near each other, so each transcript
chunk and each ledger item gets a vector, and a search asks both indexes and
merges the two rankings.

Split of work: the embedding model turns text into numbers; everything else
here is code (storage, cosine similarity, rank fusion). If no embedding model
is reachable, or a call fails, search quietly stays keyword-only.

Which model: a small local one through Ollama (nomic-embed-text, 270 MB) by
default. I tried Gemini's embedding API first; its free tier allows 1,000
texts a day, and this corpus has 7,555, with every search costing one more.
Embedding is the one job here where a local model is the better tool: it is
free, has no quota, and the corpus never leaves the machine.

Vectors are 256 floats, unit length, stored as blobs in SQLite. Searches are
nearly always inside one team (a few hundred vectors), so a plain loop is fast
enough; at 10,000 meetings this table is what a vector index would replace.
"""
import json
import os
import urllib.error
import urllib.request
from array import array

from . import store

DIMS = 256
BATCH = 32
RRF_K = 60          # reciprocal rank fusion constant; the usual default

EMBEDDERS = {
    # nomic-embed-text is trained with these prefixes and to be cut to a shorter length.
    "ollama": {"base": "http://localhost:11434/v1", "key_env": None, "model": "nomic-embed-text",
               "doc": "search_document: ", "query": "search_query: "},
    "gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "key_env": "GEMINI_API_KEY",
               "model": "gemini-embedding-001", "doc": "", "query": ""},
}


class Embedder:
    def __init__(self, name):
        cfg = EMBEDDERS[name]
        self.name, self.cfg = name, cfg
        self.model = os.environ.get("EMBED_MODEL", cfg["model"])
        self.key = os.environ.get(cfg["key_env"], "") if cfg["key_env"] else "local"

    def embed(self, texts, kind="doc"):
        """Texts -> unit-length vectors of DIMS floats."""
        body = {"model": self.model, "input": [self.cfg[kind] + t for t in texts]}
        if self.name != "ollama":
            body["dimensions"] = DIMS
        req = urllib.request.Request(
            self.cfg["base"] + "/embeddings", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "meetmem/0.1"},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))["data"]
        out = []
        for d in sorted(data, key=lambda d: d.get("index", 0)):
            v = d["embedding"][:DIMS]  # models trained for it keep their meaning when cut short
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


_embedder = False   # False = not looked for yet; None = looked and found nothing
_cache = {}         # query text -> vector, so a repeated search costs nothing
_warned = set()


def embedder():
    """The embedding model to use, or None. Checked once per process."""
    global _embedder
    if _embedder is False:
        _embedder = None
        name = os.environ.get("EMBED_PROVIDER", "ollama")
        if name in EMBEDDERS:
            try:
                e = Embedder(name)
                e.embed(["ready"])  # one tiny call proves the model is installed and running
                _embedder = e
            except Exception:
                pass
    return _embedder


def ensure(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS vectors(kind TEXT, ref INTEGER, team TEXT, meeting_id TEXT, vec BLOB, "
        "PRIMARY KEY(kind, ref))"
    )
    db.execute("CREATE INDEX IF NOT EXISTS vectors_team ON vectors(kind, team)")
    db.execute("CREATE TABLE IF NOT EXISTS vector_meta(key TEXT PRIMARY KEY, value TEXT)")


def available(db):
    """True if stored vectors exist and were made by the model that is running now.
    Vectors from different models are not comparable, so a mismatch means keyword-only."""
    ensure(db)
    e = embedder()
    row = db.execute("SELECT value FROM vector_meta WHERE key='model'").fetchone()
    return bool(e and row and row[0] == f"{e.name}/{e.model}/{DIMS}")


def missing(db):
    """(kind, ref, team, meeting, text) for everything not yet embedded."""
    ensure(db)
    items = db.execute(
        "SELECT 'item', i.item_id, i.team, i.meeting_id, i.topic || ': ' || i.statement FROM items i "
        "WHERE i.item_id NOT IN (SELECT ref FROM vectors WHERE kind='item')"
    ).fetchall()
    chunks = db.execute(
        "SELECT 'chunk', c.rowid, c.team, c.meeting_id, c.text FROM chunks_fts c "
        "WHERE c.rowid NOT IN (SELECT ref FROM vectors WHERE kind='chunk')"
    ).fetchall()
    return [tuple(r) for r in items + chunks]


def build(db, log=print):
    """Embed whatever is new. Resumable: each batch is saved as it comes back."""
    e = embedder()
    if e is None:
        log("  no embedding model reachable (start Ollama and run `ollama pull nomic-embed-text`); "
            "search stays keyword-only")
        return 0
    ensure(db)
    tag = f"{e.name}/{e.model}/{DIMS}"
    row = db.execute("SELECT value FROM vector_meta WHERE key='model'").fetchone()
    has_vectors = db.execute("SELECT 1 FROM vectors LIMIT 1").fetchone() is not None
    if has_vectors and (not row or row[0] != tag):
        # Vectors from another model (or of unknown origin) cannot be compared with this one's.
        log(f"  embedding model changed ({row[0] if row else 'unknown'} -> {tag}); re-embedding everything")
        with db:
            db.execute("DELETE FROM vectors")
    with db:
        db.execute("INSERT OR REPLACE INTO vector_meta VALUES('model', ?)", (tag,))
    todo, done = missing(db), 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        try:
            vecs = e.embed([r[4][:4000] for r in batch])
        except Exception as err:
            # Partial coverage is fine: what is embedded is searched by meaning, the rest by
            # keyword, and the next run carries on from here.
            log(f"  stopped after {done} of {len(todo)}: {str(err)[:300]}")
            return done
        with db:
            db.executemany(
                "INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)",
                [(k, ref, team, mid, array("f", v).tobytes()) for (k, ref, team, mid, _), v in zip(batch, vecs)],
            )
        done += len(batch)
        if (start // BATCH) % 25 == 0:
            log(f"  embedded {done}/{len(todo)}")
    return done


def forget(db, meeting_id):
    ensure(db)
    db.execute("DELETE FROM vectors WHERE meeting_id=?", (meeting_id,))


def _query_vec(text):
    if text not in _cache:
        if len(_cache) > 500:
            _cache.clear()
        _cache[text] = array("f", embedder().embed([text], kind="query")[0])
    return _cache[text]


def nearest(db, kind, query, team=None, meeting=None, k=20):
    """Refs of the `k` stored vectors closest in meaning to the query, best first."""
    q = _query_vec(query)
    sql, args = "SELECT ref, vec FROM vectors WHERE kind=?", [kind]
    if meeting:
        sql, args = sql + " AND meeting_id=?", args + [meeting]
    elif team:
        sql, args = sql + " AND team=?", args + [team]
    scored = []
    for ref, blob in db.execute(sql, args):
        v = array("f")
        v.frombytes(blob)
        scored.append((sum(a * b for a, b in zip(q, v)), ref))
    scored.sort(reverse=True)
    return [ref for _, ref in scored[:k]]


def fuse(*rankings, k=10):
    """Reciprocal rank fusion: a result near the top of either list ends up near the top."""
    score = {}
    for ranking in rankings:
        for rank, ref in enumerate(ranking):
            score[ref] = score.get(ref, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(score, key=score.get, reverse=True)[:k]


def _hybrid(db, kind, query, keyword_refs, team, meeting, k):
    """Keyword ranking fused with the meaning ranking, or the keyword ranking alone if
    embeddings are unavailable. Never raises: a search must not fail because of this."""
    try:
        if not query.strip() or not available(db):
            return keyword_refs[:k], "keyword"
        return fuse(keyword_refs, nearest(db, kind, query, team, meeting), k=k), "keyword + meaning"
    except Exception as e:
        if "embed" not in _warned:
            _warned.add("embed")
            print(f"    meaning search unavailable ({str(e)[:80]}); using keywords only")
        return keyword_refs[:k], "keyword"


def search_chunks(db, query, team=None, meeting=None, k=5):
    """Transcript passages for a query -> (rows, mode)."""
    kw = store.search_chunks(db, query, team, meeting, k=20)
    refs, mode = _hybrid(db, "chunk", query, [r["rowid"] for r in kw], team, meeting, k)
    if mode == "keyword":
        return kw[:k], mode
    rows = {r["rowid"]: r for r in db.execute(
        f"SELECT rowid, * FROM chunks_fts WHERE rowid IN ({','.join('?' * len(refs))})", refs)} if refs else {}
    return [rows[r] for r in refs if r in rows], mode


def search_items(db, query, team=None, meeting=None, kind=None, k=10):
    """Ledger items for a query -> (rows, mode)."""
    kw = store.search_items(db, query, team, meeting, kind, k=25)
    refs, mode = _hybrid(db, "item", query, [r["item_id"] for r in kw], team, meeting, k * 3 if kind else k)
    if mode == "keyword":
        return kw[:k], mode
    rows = {r["item_id"]: r for r in db.execute(
        f"SELECT * FROM items WHERE item_id IN ({','.join('?' * len(refs))})", refs)} if refs else {}
    out = [rows[r] for r in refs if r in rows and (not kind or rows[r]["kind"] == kind)]
    return out[:k], mode
