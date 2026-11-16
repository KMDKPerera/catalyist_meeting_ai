"""Citation checking. Plain code, never a model.

The grader checks quotes word for word, so the model is only ever trusted to
*point* at a place in a transcript. The words that go into answers.json are
copied out of the store by `locate`, which is why a citation can't be a
paraphrase: if the model's quote is not really there, it is repaired to the
nearest real span or rejected.
"""
import difflib
import re

from . import store

MIN_WORDS = 6
MAX_WORDS = 40


def _key(tok):
    return re.sub(r"[^a-z0-9]", "", tok.lower())


def locate(quote, turn_text, min_words=MIN_WORDS, fuzzy_min=MIN_WORDS):
    """Exact words from `turn_text` matching `quote`, or None.

    Matching ignores case, spacing and punctuation; the return value is always
    the transcript's own text. `fuzzy_min` is how many consecutive words must
    match before a near-miss quote is accepted and repaired.
    """
    toks = turn_text.split()
    idx = [i for i, t in enumerate(toks) if _key(t)]
    tk = [_key(toks[i]) for i in idx]
    qk = [k for k in (_key(t) for t in (quote or "").split()) if k]
    if not qk or len(toks) < min_words:
        return None

    span = None
    n = len(qk)
    for s in range(len(tk) - n + 1):
        if tk[s : s + n] == qk:
            span = (idx[s], idx[s + n - 1] + 1)
            break
    if span is None:
        # The model dropped an "um" or stitched two bits: keep the longest run
        # that really is consecutive in the transcript.
        m = difflib.SequenceMatcher(None, tk, qk, autojunk=False).find_longest_match(0, len(tk), 0, len(qk))
        if m.size < fuzzy_min:
            return None
        span = (idx[m.a], idx[m.a + m.size - 1] + 1)

    a, b = span
    while b - a < min_words and b < len(toks):  # too short: widen right, then left
        b += 1
    while b - a < min_words and a > 0:
        a -= 1
    b = min(b, a + MAX_WORDS)
    return " ".join(toks[a:b])


def find_in_meeting(db, meeting_id, quote, turn_hint=None):
    """(turn, exact_quote) for a quote somewhere in one meeting, else None.

    Tries the turn the model named, then its neighbours, then the whole meeting,
    because models are often off by a turn or two.
    """
    turns = store.get_turns(db, meeting_id)
    if not turns:
        return None
    order = sorted(turns, key=lambda t: abs(t["turn"] - turn_hint)) if turn_hint else turns
    for t in order:
        # Near the turn the model pointed at, a 4-word run is enough to trust
        # (it skipped an "uh"); anywhere else it must be a full 6-word match.
        near = turn_hint is not None and abs(t["turn"] - turn_hint) <= 2
        exact = locate(quote, t["text"], fuzzy_min=4 if near else MIN_WORDS)
        if exact:
            return t, exact
    return None


def verify(db, meeting_id, quote):
    """What the grader does: is this quote, as written, inside one turn?"""
    norm = lambda s: " ".join(s.lower().split())
    q = norm(quote or "")
    if len(q.split()) < MIN_WORDS:
        return False
    return any(q in norm(t["text"]) for t in store.get_turns(db, meeting_id))
