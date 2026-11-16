"""Does meaning search help retrieval? No model calls except the local embedder.

For each of my eval questions, the gold evidence quotes are known. A question
"finds" a quote when one of the top-k transcript passages for the question
contains it. Compared: keyword search alone vs keyword + meaning.

Run: python eval/retrieval_check.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from meetmem import embed, store  # noqa: E402

K = 8


def norm(s):
    return " ".join(s.lower().split())


def found(rows, ev):
    return any(r["meeting_id"] == ev["meeting_id"] and norm(ev["quote"]) in norm(r["text"]) for r in rows)


def main():
    db = store.connect()
    if not embed.available(db):
        raise SystemExit("Meaning search is not set up: start Ollama and run python main.py embed")
    qs = json.loads((ROOT / "eval" / "my_questions.json").read_text(encoding="utf-8"))
    tot = kw_hits = hy_hits = 0
    print(f"{'question':<6} {'evidence':<9} keyword  +meaning")
    for q in qs:
        kw = store.search_chunks(db, q["question"], q["team"], k=K)
        hy, _ = embed.search_chunks(db, q["question"], q["team"], k=K)
        for ev in q["evidence"]:
            a, b = found(kw, ev), found(hy, ev)
            tot += 1
            kw_hits += a
            hy_hits += b
            mark = "  <- gained" if b and not a else ("  <- lost" if a and not b else "")
            print(f"{q['id']:<6} {ev['meeting_id']:<9} {'yes' if a else ' - ':<8} {'yes' if b else ' - '}{mark}")
    print(f"\nGold evidence in the top {K} passages: keyword {kw_hits}/{tot}, keyword + meaning {hy_hits}/{tot}")


if __name__ == "__main__":
    main()
