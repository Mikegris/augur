"""Semantic memory for Jarvis — embedding-based recall over durable facts.

Jarvis accumulates two kinds of long-lived text: durable facts in
jarvis_memory ("user prefers dividend payers") and conversation summaries.
Keyword search over those breaks down fast — "what did we conclude about
IONQ last month" shares almost no tokens with "quantum position sized down
after the Q3 dilution". This module stores an OpenAI embedding next to each
text and answers recall queries by cosine similarity, so retrieval works on
meaning rather than literal word overlap.

Design decisions, and why:

- Full-scan cosine, no ANN index. The corpus is Jarvis's own memories and
  summaries — hundreds of rows, a few thousand at the very worst. A numpy
  matrix-vector product over that is sub-millisecond; FAISS/sqlite-vss would
  add a dependency and an index to keep consistent for zero perceptible
  speedup at this scale.
- float32 BLOBs via array('f'). text-embedding-3-small is 1536 dims; float64
  would double storage for precision cosine similarity cannot use. 6 KB/row
  keeps wealth.db small even if the corpus grows 10x.
- Lazy table creation + lazy imports. The module must import cleanly in a
  keyless or partial install (numpy/openai missing), so nothing heavy runs
  at import time and the table appears only on first real use.
- Fail-open everywhere. Semantic recall is an enhancement, never a load-
  bearing path: a missing key, network blip, or numpy hiccup degrades to
  "no recall" (empty list / False / None vectors), never an exception in
  the caller.
"""

import logging
from array import array

log = logging.getLogger(__name__)

# Model + dimensionality are fixed together: vectors from different models
# are not comparable, so changing the model means re-embedding the corpus.
EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

# Rows scoring below this are noise — empirically, unrelated texts land
# around 0.0-0.25 cosine with text-embedding-3-small, genuinely related
# ones 0.4+. 0.30 keeps borderline-relevant hits without surfacing junk.
MIN_SCORE = 0.30

# Hard cap on stored text. Embeddings of very long texts get muddy (the
# vector averages over too many topics) and tokens cost money; 1000 chars
# comfortably covers a fact or a conversation summary.
MAX_TEXT_CHARS = 1000

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jarvis_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    text TEXT NOT NULL,
    vec BLOB NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(kind, ref_id)
)
"""


def _ensure_table():
    """Create the embeddings table on first use. DDL is a write, so it
    goes under the shared write lock like every other mutation."""
    import database as db
    with db._write_lock:
        db.get_conn().execute(_TABLE_SQL)
        db.get_conn().commit()


def _to_blob(vec):
    """float32-pack a vector. array('f') is stdlib, allocation-free
    relative to struct.pack with 1536 format chars, and round-trips
    exactly through _from_blob."""
    return array("f", vec).tobytes()


def _from_blob(blob):
    a = array("f")
    a.frombytes(blob)
    return a


def embed(texts):
    """Embed a batch of texts in ONE API call.

    Returns a list the same length as `texts`; every element is either a
    list of floats or None. All-None on any failure (keyless, network,
    malformed response) — callers treat None as "no vector", never retry
    here.
    """
    texts = list(texts or [])
    if not texts:
        return []
    failed = [None] * len(texts)
    try:
        import ai_summarizer
        key = ai_summarizer.get_openai_key()
        if not key:
            return failed
        # Respect the app-wide daily AI cap: embeddings are paid calls like
        # any other, and recall silently degrading is exactly the designed
        # behavior when the budget is spent.
        try:
            if ai_summarizer._cap_exceeded():
                return failed
        except Exception:
            pass  # cap check is best-effort; don't let it block embedding
        # The API rejects empty strings; substitute a single space so one
        # blank entry can't sink the whole batch, and cap length so a huge
        # text can't blow the token limit for everyone else in the batch.
        safe = [(t or " ").strip()[:MAX_TEXT_CHARS] or " " for t in texts]
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=10)
        resp = client.embeddings.create(model=EMBED_MODEL, input=safe)
        # The request was billed the moment it returned, regardless of shape —
        # record it BEFORE validating length so a malformed/partial response
        # can't slip through as "free" and under-count spend against the cap.
        ai_summarizer._record_ai_call()  # one batched request = one call
        if not resp.data or len(resp.data) != len(texts):
            return failed
        # API may return out of order in theory; .index makes it explicit.
        out = [None] * len(texts)
        for item in resp.data:
            out[item.index] = list(item.embedding)
        return out
    except Exception as e:
        log.debug("embed failed: %s", e)
        return failed


def store(kind, ref_id, text):
    """Embed `text` and upsert it under (kind, ref_id). Returns True only
    when a vector was actually written. Keyless / empty text / any error
    → False, silently — storing memories must never break the caller."""
    try:
        text = (text or "").strip()[:MAX_TEXT_CHARS]
        if not text:
            return False
        vecs = embed([text])
        vec = vecs[0] if vecs else None
        if vec is None:
            return False  # keyless or embed failure — nothing to persist
        import database as db
        _ensure_table()
        with db._write_lock:
            # INSERT OR REPLACE: re-storing the same (kind, ref_id) — e.g.
            # an edited memory — should refresh both text and vector, not
            # accumulate stale duplicates.
            db.get_conn().execute(
                "INSERT OR REPLACE INTO jarvis_embeddings (kind, ref_id, text, vec)"
                " VALUES (?,?,?,?)",
                (str(kind), str(ref_id), text, _to_blob(vec)))
            db.get_conn().commit()
        return True
    except Exception as e:
        log.debug("semmem store failed: %s", e)
        return False


def forget(kind, ref_id):
    """Delete the embedding mirror for (kind, ref_id). Returns True when a
    row was removed. Without this, "forget N" drops the jarvis_memory row but
    leaves its embedding behind, so recall() re-injects the "forgotten" fact.
    Fail-open: any error returns False rather than breaking the forget path."""
    try:
        import database as db
        _ensure_table()
        with db._write_lock:
            cur = db.get_conn().execute(
                "DELETE FROM jarvis_embeddings WHERE kind=? AND ref_id=?",
                (str(kind), str(ref_id)))
            db.get_conn().commit()
            return cur.rowcount > 0
    except Exception as e:
        log.debug("semmem forget failed: %s", e)
        return False


def recall(query, k=6, kinds=None):
    """Top-k semantically similar rows for `query`.

    Returns [{"text", "kind", "ref_id", "score"}, ...] sorted by score
    descending, score >= MIN_SCORE only. Empty list on any failure.

    Full scan is deliberate: the corpus is Jarvis's own memories (at most
    a few thousand rows), so loading every vector into one numpy matrix
    and doing a single mat-vec is both the simplest and the fastest thing
    that can work here.
    """
    try:
        query = (query or "").strip()
        if not query or k <= 0:
            return []
        qvecs = embed([query])
        qvec = qvecs[0] if qvecs else None
        if qvec is None:
            return []
        import database as db
        _ensure_table()
        sql = "SELECT kind, ref_id, text, vec FROM jarvis_embeddings"
        params = []
        if kinds:
            sql += " WHERE kind IN (%s)" % ",".join("?" * len(kinds))
            params = [str(x) for x in kinds]
        rows = db.get_conn().execute(sql, params).fetchall()
        if not rows:
            return []
        import numpy as np  # lazy: keep module importable without numpy
        dim = len(qvec)
        cands = []  # (row, vector) pairs whose dimensionality matches
        for r in rows:
            v = _from_blob(r["vec"])
            if len(v) == dim:  # skip rows from a different/older model
                cands.append((r, v))
        if not cands:
            return []
        mat = np.array([v for _, v in cands], dtype=np.float32)
        q = np.array(qvec, dtype=np.float32)
        # Cosine = dot / (|a||b|). OpenAI vectors arrive ~unit-norm, but
        # normalize explicitly so a stale or truncated row can't produce
        # a bogus >1 score.
        qn = float(np.linalg.norm(q))
        if not np.isfinite(qn) or qn == 0.0:
            return []  # zero/NaN query vector → every score is 0/NaN, no recall
        norms = np.linalg.norm(mat, axis=1) * qn
        norms[norms == 0] = 1.0  # guard zero vectors → score 0, not NaN
        scores = np.nan_to_num((mat @ q) / norms)  # corrupt rows → 0, not NaN
        order = np.argsort(scores)[::-1]
        out = []
        for i in order:
            s = float(scores[i])
            if not np.isfinite(s):
                continue  # never break on a non-finite score; just skip it
            if s < MIN_SCORE:
                break  # sorted desc — everything after is below threshold
            r = cands[i][0]
            out.append({"text": r["text"], "kind": r["kind"],
                        "ref_id": r["ref_id"], "score": round(s, 4)})
            if len(out) >= k:
                break
        return out
    except Exception as e:
        log.debug("semmem recall failed: %s", e)
        return []


def index_memories():
    """Backfill: embed every jarvis_memory fact not yet in the table
    (kind="memory", ref_id=str(id)). Returns the number newly stored.
    Idempotent — safe to call on every startup or after bulk imports."""
    try:
        import database as db
        _ensure_table()
        have = set(
            r["ref_id"] for r in db.get_conn().execute(
                "SELECT ref_id FROM jarvis_embeddings WHERE kind='memory'"
            ).fetchall())
        added = 0
        for mem in db.jarvis_list_memories():
            ref = str(mem["id"])
            if ref in have:
                continue
            # store() embeds one row per call here rather than batching the
            # whole backlog: simpler, and backfill happens rarely on a small
            # corpus. If it ever matters, batch via embed() directly.
            if store("memory", ref, mem["fact"]):
                added += 1
        return added
    except Exception as e:
        log.debug("semmem index_memories failed: %s", e)
        return 0


def stats():
    """Cheap health probe for the UI/diagnostics: row count + whether an
    API key is configured (ready = recall can actually work)."""
    rows = 0
    try:
        import database as db
        _ensure_table()
        rows = db.get_conn().execute(
            "SELECT COUNT(*) AS n FROM jarvis_embeddings").fetchone()["n"]
    except Exception as e:
        log.debug("semmem stats failed: %s", e)
    ready = False
    try:
        import ai_summarizer
        ready = bool(ai_summarizer.get_openai_key())
    except Exception:
        pass
    return {"rows": int(rows), "ready": ready}
