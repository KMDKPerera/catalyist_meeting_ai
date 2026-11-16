"""Memory store: SQLite + FTS5 (BM25).

Everything in this file is plain code. Parsing, chunking, indexing, speaker
attribution, time ordering and "what is current" lookups are deterministic, so
no model is involved.
"""
import re
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings(
    meeting_id TEXT PRIMARY KEY, team TEXT, idx INTEGER, n_turns INTEGER, n_words INTEGER);
CREATE TABLE IF NOT EXISTS turns(
    meeting_id TEXT, turn INTEGER, t TEXT, speaker TEXT, text TEXT,
    PRIMARY KEY(meeting_id, turn));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, extra, meeting_id UNINDEXED, team UNINDEXED, idx UNINDEXED,
    turn_start UNINDEXED, turn_end UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS roles(
    meeting_id TEXT, speaker TEXT, role TEXT, PRIMARY KEY(meeting_id, speaker));
CREATE TABLE IF NOT EXISTS items(
    item_id INTEGER PRIMARY KEY, meeting_id TEXT, team TEXT, idx INTEGER, turn INTEGER,
    t TEXT, speaker TEXT, kind TEXT, topic TEXT, statement TEXT, owner TEXT, quote TEXT,
    thread_id INTEGER);
CREATE INDEX IF NOT EXISTS items_team ON items(team, idx, turn);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    text, item_id UNINDEXED, team UNINDEXED, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS threads(
    thread_id INTEGER PRIMARY KEY, team TEXT, topic TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS supersedes(
    old_id INTEGER, new_id INTEGER, PRIMARY KEY(old_id, new_id));
CREATE TABLE IF NOT EXISTS extracted(
    meeting_id TEXT, seg INTEGER, PRIMARY KEY(meeting_id, seg));
CREATE TABLE IF NOT EXISTS reconciled(team TEXT PRIMARY KEY, n_items INTEGER);
CREATE TABLE IF NOT EXISTS llm_calls(
    ts REAL, purpose TEXT, model TEXT, prompt_tokens INTEGER,
    completion_tokens INTEGER, seconds REAL);
"""

HEADER_RE = re.compile(r"^(\d\d:\d\d:\d\d) Speaker ([A-Z])$")
META_RE = re.compile(r"^(Meeting|Team|Meeting number)\s*:\s*(.+)$", re.I)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,39}$")  # also keeps ids safe to use as file names
TEAM_RE = re.compile(r"\b((?:ES|IS|TS)\d{4})([a-d])?\b")

FORMAT_HELP = """Expected transcript format (plain text, UTF-8):

    Meeting: ACME7c            <- optional; defaults to the file name
    Team: ACME7                <- optional if the id is the team name plus one letter
    Meeting number: 3          <- optional if the id ends in a letter (a = 1, b = 2, ...)

    00:00:02 Speaker A
    The words spoken in this turn, on one line.

    00:00:09 Speaker B
    The next turn.

Each turn is a line "HH:MM:SS Speaker X" (X is one capital letter) followed by its text."""


class TranscriptError(ValueError):
    """The file cannot be added as it is; the message says what to change."""
# The corpus spells acronyms letter by letter ("L_C_D_", "T_V_"), which a
# tokenizer splits into single letters, so a search for "LCD" finds nothing.
# The joined form is indexed next to the text; the text itself is untouched.
ACRONYM_RE = re.compile(r"\b(?:[A-Za-z]_){2,}")

STOP = set(
    "a an and are as at be been but by did do does for from had has have he her his how i if in "
    "into is it its me my of on or our she so than that the their them then there these they this "
    "to up us was we were what when where which who whom why will with would you your team".split()
)


def connect(path=None):
    db = sqlite3.connect(str(path or config.DB_PATH), timeout=120)  # several ingest workers may write
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------- ingest

def parse_text(text):
    """Transcript text -> (header fields, turns). A turn is a header line then its text."""
    lines = text.splitlines()
    meta, turns, i = {}, [], 0
    while i < len(lines):
        m = HEADER_RE.match(lines[i].strip())
        if not m:
            meta_line = META_RE.match(lines[i].strip()) if not turns else None
            if meta_line:
                meta.setdefault(meta_line.group(1).lower(), meta_line.group(2).strip())
            i += 1
            continue
        i += 1
        body = []
        while i < len(lines) and lines[i].strip() and not HEADER_RE.match(lines[i].strip()):
            body.append(lines[i].strip())
            i += 1
        text = " ".join(body)
        if text:
            turns.append({"turn": len(turns) + 1, "t": m.group(1), "speaker": m.group(2), "text": text})
    return meta, turns


def describe(text, fallback_id=None, team=None, number=None):
    """Work out what a transcript is (id, team, place in the team's order) and check it.

    Explicit arguments win, then the file's own header lines, then the id itself
    ("ACME7c" = team ACME7, meeting 3). Raises TranscriptError with a fixable message.
    """
    meta, turns = parse_text(text)
    if not turns:
        raise TranscriptError("No turns found.\n\n" + FORMAT_HELP)
    mid = (meta.get("meeting") or "").strip()
    if not mid:  # fall back to the file name: "Wednesday sync" becomes "Wednesday-sync"
        mid = re.sub(r"[^A-Za-z0-9_-]+", "-", fallback_id or "").strip("-")[:40]
    if not ID_RE.match(mid):
        raise TranscriptError(f"Meeting id {mid!r} is not usable: use letters, digits, '-' or '_' (2 to 40 characters).")
    # "TS3012c" reads as team TS3012, third meeting. Only when a digit comes before the
    # letter, so a file called "notes" is not taken for meeting 19 of team "note".
    letter = mid[-1] if re.match(r"^.*\d[a-z]$", mid) else None
    team = (team or meta.get("team") or (mid[:-1] if letter else "")).strip()
    if not ID_RE.match(team):
        raise TranscriptError(f"Cannot tell which team {mid} belongs to. Add a 'Team: NAME' line or pass the team.")
    if number is None:
        lead = re.match(r"\d+", meta.get("meeting number", ""))
        number = int(lead.group()) if lead else (ord(letter) - ord("a") + 1 if letter else None)
    if not number or number < 1:
        raise TranscriptError(
            f"Cannot tell where {mid} comes in team {team}'s order. Add a 'Meeting number: N' line or pass the number."
        )
    return {"meeting_id": mid, "team": team, "idx": int(number), "turns": turns,
            "words": sum(len(t["text"].split()) for t in turns),
            "speakers": sorted({t["speaker"] for t in turns})}


def add_meeting(db, info):
    """Store one described meeting: turns plus search index. No model involved."""
    mid, team, idx, turns = info["meeting_id"], info["team"], info["idx"], info["turns"]
    if db.execute("SELECT 1 FROM meetings WHERE meeting_id=?", (mid,)).fetchone():
        raise TranscriptError(f"{mid} is already in the store. Use replace to load a corrected version.")
    clash = db.execute("SELECT meeting_id FROM meetings WHERE team=? AND idx=?", (team, idx)).fetchone()
    if clash:
        raise TranscriptError(
            f"Team {team} already has a meeting number {idx} ({clash[0]}). Time order is what tells the "
            "store which decision is current, so each meeting needs its own number."
        )
    with db:
        db.execute("INSERT INTO meetings VALUES(?,?,?,?,?)", (mid, team, idx, len(turns), info["words"]))
        db.executemany(
            "INSERT INTO turns VALUES(?,?,?,?,?)",
            [(mid, t["turn"], t["t"], t["speaker"], t["text"]) for t in turns],
        )
        for c in chunk_turns(turns):
            text = "\n".join(fmt_turn(t) for t in c)
            extra = " ".join(a.replace("_", "") for a in ACRONYM_RE.findall(text))
            db.execute(
                "INSERT INTO chunks_fts VALUES(?,?,?,?,?,?,?)",
                (text, extra, mid, team, idx, c[0]["turn"], c[-1]["turn"]),
            )


def remove_meeting(db, meeting_id):
    """Take a meeting's transcript out of the store (its ledger items are removed by extract.forget)."""
    with db:
        db.execute("DELETE FROM chunks_fts WHERE meeting_id=?", (meeting_id,))
        db.execute("DELETE FROM turns WHERE meeting_id=?", (meeting_id,))
        db.execute("DELETE FROM meetings WHERE meeting_id=?", (meeting_id,))


def chunk_turns(turns, words=config.CHUNK_WORDS, overlap=2):
    """Windows of whole turns, about `words` long, sharing `overlap` turns."""
    out, cur, n = [], [], 0
    for t in turns:
        cur.append(t)
        n += len(t["text"].split())
        if n >= words:
            out.append(cur)
            cur = cur[-overlap:]
            n = sum(len(x["text"].split()) for x in cur)
    if cur and (not out or cur[-1] is not out[-1][-1]):
        out.append(cur)
    return out


def fmt_turn(t):
    return f"[{t['turn']}] {t['speaker']}: {t['text']}"


def ingest(db, data_dir=None, log=print):
    """Add any transcript in the folder that is not yet in the store. Safe to re-run every day:
    drop new files into data/transcripts and run it again."""
    added = 0
    known = {r[0] for r in db.execute("SELECT meeting_id FROM meetings")}
    for path in sorted((data_dir or config.DATA / "transcripts").glob("*.txt")):
        if path.stem in known:
            continue
        try:
            add_meeting(db, describe(path.read_text(encoding="utf-8"), path.stem))
            added += 1
        except TranscriptError as e:  # one bad file must not block the rest
            log(f"  skipped {path.name}: {str(e).splitlines()[0]}")
    return added


# ---------------------------------------------------------------- lookups

def fts_query(q):
    words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in STOP and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def find_team(text, db=None):
    """Team named in a question, e.g. 'Team TS3012 ...' -> 'TS3012'.

    With a store, any team in it is recognised by name, so teams added later
    need no code change. Without one, falls back to the AMI id pattern.
    """
    text = text or ""
    if db is not None:
        # The name on its own, or with one trailing letter as in a meeting id ("TS3012c").
        hits = [
            t for (t,) in db.execute("SELECT DISTINCT team FROM meetings")
            if re.search(r"(?<![A-Za-z0-9_-])" + re.escape(t) + r"[a-z]?(?![A-Za-z0-9_-])", text, re.I)
        ]
        if hits:
            return max(hits, key=len)
    m = TEAM_RE.search(text)
    return m.group(1) if m else None


def meeting_index(db, ref):
    """'TS3012c', 'c' or '3' -> the meeting's place in its team's order (for "as of" views)."""
    if not ref:
        return None
    row = db.execute("SELECT idx FROM meetings WHERE meeting_id=?", (ref,)).fetchone()
    if row:
        return row[0]
    if str(ref).isdigit():
        return int(ref)
    if len(ref) == 1 and ref.isalpha():
        return ord(ref.lower()) - ord("a") + 1
    raise ValueError(f"unknown meeting {ref!r}")


def get_turns(db, meeting_id, start=1, end=None):
    end = end or 10**9
    return db.execute(
        "SELECT * FROM turns WHERE meeting_id=? AND turn BETWEEN ? AND ? ORDER BY turn",
        (meeting_id, start, end),
    ).fetchall()


def get_roles(db, meeting_id):
    """Roles for the speakers of one meeting, resolved across the whole team (see team_roles)."""
    row = db.execute("SELECT team FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
    return team_roles(db, row["team"]) if row else {}


def team_roles(db, team):
    """Speaker letter -> role, kept only where the team's meetings agree.

    The model reads a role off each meeting separately and is wrong often enough
    to matter (39% of speaker letters had two different roles across a team's
    meetings). So code takes a vote: a letter gets a role when at least two
    meetings say so and no other role comes close, and two letters may not end
    up with the same role. Anything less certain is left out, because a missing
    role is harmless and a wrong one puts an error into an answer.
    """
    votes = {}
    for r in db.execute(
        "SELECT r.speaker, r.role, COUNT(*) n FROM roles r JOIN meetings m ON m.meeting_id=r.meeting_id "
        "WHERE m.team=? GROUP BY r.speaker, r.role", (team,)
    ):
        votes.setdefault(r["speaker"], []).append((r["n"], r["role"]))
    picked = {}
    for speaker, counts in votes.items():
        counts.sort(reverse=True)
        top, runner_up = counts[0], (counts[1][0] if len(counts) > 1 else 0)
        if top[0] >= 2 and top[0] >= 2 * runner_up + 1:
            picked[speaker] = top[1]
    clash = {role for role in picked.values() if list(picked.values()).count(role) > 1}
    return {s: role for s, role in sorted(picked.items()) if role not in clash}


def search_chunks(db, query, team=None, meeting=None, k=5):
    fq = fts_query(query)
    if not fq:
        return []
    sql = "SELECT rowid, *, bm25(chunks_fts) AS score FROM chunks_fts WHERE chunks_fts MATCH ?"
    args = [fq]
    if meeting:
        sql += " AND meeting_id=?"
        args.append(meeting)
    elif team:
        sql += " AND team=?"
        args.append(team)
    return db.execute(sql + " ORDER BY score LIMIT ?", args + [k]).fetchall()


def search_items(db, query, team=None, meeting=None, kind=None, k=10):
    fq = fts_query(query)
    if not fq:
        return []
    sql = (
        "SELECT i.*, bm25(items_fts) AS score FROM items_fts f JOIN items i ON i.item_id=f.item_id "
        "WHERE items_fts MATCH ?"
    )
    args = [fq]
    if meeting:
        sql += " AND i.meeting_id=?"
        args.append(meeting)
    elif team:
        sql += " AND i.team=?"
        args.append(team)
    if kind:
        sql += " AND i.kind=?"
        args.append(kind)
    return db.execute(sql + " ORDER BY score LIMIT ?", args + [k]).fetchall()


def superseded_by(db, item_id, as_of=None):
    """The later item that replaced this one, as known at meeting index `as_of`."""
    row = db.execute(
        "SELECT n.item_id, n.meeting_id FROM supersedes s JOIN items n ON n.item_id=s.new_id "
        "WHERE s.old_id=? AND n.idx<=? ORDER BY n.idx, n.turn LIMIT 1",
        (item_id, as_of or 10**9),
    ).fetchone()
    return row


def fmt_item(db, it, as_of=None):
    """One ledger line the agent (or a human) can read."""
    flag = ""
    sup = superseded_by(db, it["item_id"], as_of)
    if sup:
        flag = f" [SUPERSEDED by #{sup['item_id']} in {sup['meeting_id']}]"
    owner = f" owner={it['owner']}" if it["owner"] else ""
    return (
        f"#{it['item_id']} {it['meeting_id']} turn {it['turn']} Speaker {it['speaker']} "
        f"{it['kind']}{owner}{flag} | {it['topic']}: {it['statement']}"
    )
