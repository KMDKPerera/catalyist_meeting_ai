"""Adding a meeting after the first build: one path shared by the command line and the web UI.

A new transcript costs only its own work: parse and index it (code), extract its
ledger items (one to four model calls), then re-link the threads of the one team
it belongs to (one model call). Nothing else in the store is touched, so the
price of adding a meeting does not grow with the size of the corpus.
"""
from . import config, embed, extract, reconcile, store


def preview(db, text, fallback_id=None, team=None, number=None):
    """What would be added, without adding it. Raises store.TranscriptError if unusable."""
    info = store.describe(text, fallback_id, team, number)
    others = db.execute(
        "SELECT meeting_id, idx FROM meetings WHERE team=? ORDER BY idx", (info["team"],)
    ).fetchall()
    return {
        "meeting_id": info["meeting_id"], "team": info["team"], "number": info["idx"],
        "turns": len(info["turns"]), "words": info["words"], "speakers": info["speakers"],
        "exists": any(m["meeting_id"] == info["meeting_id"] for m in others),
        "team_meetings": [m["meeting_id"] for m in others],
        "new_team": not others,
        # Later meetings already on file: this one will be slotted in before them.
        "later": [m["meeting_id"] for m in others if m["idx"] > info["idx"]],
    }


def add(db, text, fallback_id=None, team=None, number=None, replace=False):
    """Validate, store and index one transcript (no model). Returns its description."""
    info = store.describe(text, fallback_id, team, number)
    mid = info["meeting_id"]
    if replace and db.execute("SELECT 1 FROM meetings WHERE meeting_id=?", (mid,)).fetchone():
        extract.forget(db, mid)
        with db:
            embed.forget(db, mid)
        store.remove_meeting(db, mid)
    store.add_meeting(db, info)
    # Keep the folder as the source of truth: memory.db can always be rebuilt from it.
    folder = config.DATA / "transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{mid}.txt").write_text(_with_header(text, info), encoding="utf-8")
    return info


def _with_header(text, info):
    """Write the id, team and number into the saved file, so a rebuild needs no arguments."""
    meta, _ = store.parse_text(text)
    lines = []
    if "meeting" not in meta:
        lines.append(f"Meeting: {info['meeting_id']}")
    if "team" not in meta:
        lines.append(f"Team: {info['team']}")
    if "meeting number" not in meta:
        lines.append(f"Meeting number: {info['idx']}")
    return ("\n".join(lines) + "\n\n" + text) if lines else text


def process(db, llm, meeting_ids, log=print):
    """Model steps for newly added meetings: extract them, then re-link only their teams."""
    ids = set(meeting_ids)
    extract.run(db, llm, log=log, only=ids)
    teams = sorted({r[0] for r in db.execute(
        f"SELECT DISTINCT team FROM meetings WHERE meeting_id IN ({','.join('?' * len(ids))})", list(ids))})
    for team in teams:
        reconcile.run(db, llm, team, log=log)
    if embed.available(db):  # keep meaning search complete if it is in use; never fatal if not
        n = embed.build(db, log=lambda *_: None)
        if n:
            log(f"  {n} new passages and items added to meaning search")
    return teams
