# Write-up: meeting memory agent

## Architecture

Three stages, each resumable and incremental per meeting.

1. **Ingest (code).** Transcripts are parsed into turns (meeting, turn number, time, speaker, text) in
   SQLite, grouped into ~200-word chunks and indexed with FTS5/BM25. 138 meetings (676,745 words, 48,148
   turns, 4,209 chunks) ingest in under 4 seconds.
2. **Memory (model, then code).** The model reads each meeting once, about 3,000 words per call, and
   writes ledger items: decision, proposal, objection, action, fact, requirement, each pointing at a
   turn. A second pass per team reads only that team's ledger and groups items into threads, with links
   saying "this later item replaces that earlier one". Result: 3,346 items, 200 threads, 211 links.
3. **Answering (agent loop).** Code does the first retrieval for free: the question's best ledger
   matches and the full history of the thread most of them belong to. The model then works with five
   tools (`search_memory`, `timeline`, `search_transcript`, `read_turns`, `answer`), choosing each lookup
   from what the last one returned. It never sees a whole transcript, so prompt size does not depend on
   corpus size. Before an answer is accepted:
   - code checks every citation word for word, pushes back once if all citations are from one meeting,
     refuses a repeated lookup, and on the last two steps offers only the `answer` tool;
   - a **second reader** (one call, fresh context, a stronger model when quota allows) checks the draft
     against the transcript around each citation and can send it back once.
4. **Two passes and a judge.** A small model's answers vary between runs, so the 25 questions were
   answered twice and a judge picked one draft per question after reading both with their evidence. The
   judge never rewrites, so citations stay the ones code verified.

**Growing every day.** `python main.py add FILE` (or the web UI's Add meeting tab, or dropping files in
the folder and re-running `build`) costs only that meeting's work: index it, extract it in 1 to 4 calls,
and re-link its one team in 1 call. I tested this on a throwaway store: adding meeting b to a team that
had only meeting a marked two of a's items as replaced by b's, and a question was then answered with
citations from the added meetings. Teams and meeting ids need not follow the AMI naming.

**Knowing what is current.** Every item carries its meeting order and turn, and supersession links are
data, so "current" is a query: an item is current as of meeting N if no link points to a replacement at
or before N. Search results show `[SUPERSEDED by #id in meeting]` inline. The brief for TS3012 is built
from the record rewound to meeting c, so it cannot leak the final meeting. `python main.py timeline
TEAM --as-of c`, or the Decision timeline tab of the web UI, shows this view.

## Model versus code

| Job | Who | Why |
| --- | --- | --- |
| Parsing, chunking, indexing, ranking, the first retrieval for a question | code | Deterministic, free, fast. |
| Who said it and when | code | Looked up from the turn the model points at. |
| The words in every citation | code | Graded word for word. The model points; code copies the text out, repairs a near miss to the real span, or rejects it. |
| Time order, "as of meeting c", refusing a replacement that is earlier than what it replaces | code | Arithmetic. Code rejected 18 backwards links the model proposed. |
| Stopping a loop that will not stop, refusing repeated lookups | code | A small model ignores "two steps left"; taking the other tools away works. |
| Is this a decision, an objection, a commitment? | model | Needs understanding of messy speech. |
| Are two items about the same thing, and does one replace the other? | model | Meaning, not string matching. |
| What to look up next, and when there is enough | model | Depends on what the last lookup returned. |
| Does the evidence support the draft? Which of two drafts is better? | model | Reading comprehension, done with a fresh context so it cannot inherit the first misreading. |
| Scoring my eval set | code | Regex key points and exact-quote checks, so the score is repeatable. |

## Platform, and where it broke

Python standard library, SQLite FTS5, Gemini through its OpenAI-compatible endpoint. No framework: the
loop is short and every step is in a trace I can read.

- **Free-tier quota decided the model.** `gemini-3.8-flash` allows 20 requests a day; it died after 13
  meetings. Bulk work moved to `gemini-3.5-flash-lite` (roughly 480 calls a day) with automatic failover
  to `gemini-3.1-flash-lite`. 124 meetings were extracted by flash-lite and 10 by 3.8-flash. The 4 TS3012
  meetings were re-extracted with `gemini-3.5-flash` because flash-lite's ledger for meeting c was too
  thin for a brief (25 items versus 49), and the brief text was written by `gemini-3.6-flash`. So the
  brief is better than flash-lite alone would give. All 25 answers come from the flash-lite agent.
- **The stronger models ran out mid-run.** The second reader and the judge are meant to use them, one
  call per question. On the day, 22 of 25 judge calls and about half the reviews fell back to flash-lite.
- **Plain-JSON tool protocol failed on Gemini.** It kept trying to emit an undeclared native function
  call and returned empty replies with `finish_reason: MALFORMED_FUNCTION_CALL`. Declared function calling fixed it.
- **Thinking models and output limits.** Linking TS3012 on `gemini-3.5-flash` failed with
  `finish_reason: length`: its reasoning used up the output budget before the JSON was complete.
- **Acronyms.** The corpus spells "L_C_D_", which FTS5 tokenizes to single letters, so "LCD" matched
  nothing. Fixed by indexing a joined form beside the text.
- **BM25 alone misses** when question and speech share no words. Search by meaning was added later
  (below), but Gemini's free embedding tier was too small, so it runs on a local model.

## Proof it works

`eval/my_questions.json`: 14 questions on teams the official questions do not use. I read the transcript
passages for each gold answer, and code confirms every gold quote is exact.

| Agent version | Original 11 | 3 held-out | Citations exact |
| --- | --- | --- | --- |
| First version | 9 of 11 | not run | 26 of 26 |
| With code-side retrieval, second reader, forced final answer | 11 of 11 | 3 of 3 | 33 of 33 |

A later round added search by meaning (a local `nomic-embed-text` model via Ollama, merged with
keyword search), speaker roles vetted by a vote across each team's meetings, and a judge that can
compare more than two passes. On a no-model retrieval check (`eval/retrieval_check.py`), the gold
evidence appears in the top 8 passages for 18 of 31 quotes with merged search against 16 with
keywords alone; at 5 passages there is no gain. Re-running the eval after that round scored 12 of 14
(34 of 34 citations exact). One miss was a vague answer and one a real error: E08 again took a running
subtotal for the final cost, even though the second reader flagged exactly that and the agent kept its
draft. Gemini's free embedding API capped at 1,000 texts a day against 7,555 needed, which is why the
embedder is local.

The improvements were developed against the original 11, so that column flatters the agent; the three
held-out questions were written afterwards and never used for tuning. Three is a small sample. The first
automatic score on the first version was 7 of 11: two correct answers failed regexes that were too narrow,
which I widened before re-scoring.

- **answers.json:** 25 of 25 answered, 64 of 64 citations exact. 13 answers came from pass 1 and 12 from
  pass 2. I checked four questions where the passes disagreed against the transcript: the judge picked
  the better draft on three (Q10, Q17, Q24) and, I believe, the worse one on Q25.
- **Cost of ingesting 1,000 meetings,** scaled from logged tokens: about 9M input and 2.1M output tokens
  for extraction plus about 1.2M input and 0.2M output for linking. At an assumed $0.30 / $2.50 per million
  tokens that is roughly $9; `python main.py stats --price-in X --price-out Y` recomputes it.
- **Time:** on flash-lite, 128 meetings took 23 minutes one call at a time, so about 3 hours per 1,000.
  On 3.8-flash each call took 35 to 160 seconds. Jobs are independent per meeting, so this parallelises.
  On the free tier it is quota-bound at roughly 200 meetings a day.
- **Asking:** 5 to 8 tool steps plus one review call, about 30,000 input tokens per question.

## Three questions it got wrong

1. **E05, ES2013: "which case material were two members both thinking of?"** First version answered
   rubber; the transcript says titanium. It keyword-searched the question's own wording, landed on the
   spongy-rubber discussion and never opened the ledger. It is now right, but only because the second
   reader sent the draft back for a different reason and the retry found the right turns. In two spot
   checks with flash-lite as the reader it stayed wrong.
2. **E08, ES2015: "what did the final design cost?"** First version answered 8 Euros; the answer is 12.20.
   It read the right 30 turns, which contain both "we're at eight" (a running subtotal) and "our operating
   cost is twelve twenty Euros", and picked the subtotal, with a real quote attached. Exact citations are
   not enough. Now right, after a prompt rule about subtotals.
3. **Q24 and E11: no answer at all.** On Q24 the first version spent ten of twelve steps re-running one
   search with the words shuffled. After I blocked repeated lookups, E11 (the false-premise question it
   had previously passed) searched for fourteen steps and never answered. Code now offers only `answer`
   on the last two steps.

Still wrong or doubtful in the submission: **Q25** (ES2011 fruit shapes), where the judge kept the pass
that describes the fruit-shaped prototype, while late in meeting d the team moves towards one shape in
different colours named after fruits. The second reader also raised one false objection (E06: it claimed
Speaker C was the user interface designer); the agent held its answer.

Wrong inside the memory itself: some flash-lite statements use first names instead of speaker letters,
and 134 of 3,346 items had quotes the checker could not find and are stored uncitable. The brief's
open-actions section misses the design sketch asked for at the end of meeting c; the fix (feeding the
last 60 turns of the latest meeting in verbatim) works, but the only model left to run it that day was
flash-lite, whose brief was weaker overall, so the submitted brief predates it (`runs/brief_v3.txt`).

## What production needs at 10,000 meetings

- Postgres with a real search engine and vector index, partitioned by team. FTS5 filters by team after matching.
- Ingest as a queue with parallel workers, and reconcile only the new meeting against its team's thread heads.
- A stronger extraction model, and a paid tier so the second reader and judge always run on one. Ledger
  quality and model strength were the ceiling on everything downstream here.
- Real dates and identities; here "a to d" gives the order and letters stand in for people.
- Human review of low-confidence supersession links: a wrong link hides a live decision.
- The eval set in CI with far more held-out questions and a model judge alongside the regexes.
