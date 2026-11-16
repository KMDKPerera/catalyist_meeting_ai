# meetmem: meeting memory agent

Catalist Agentic AI Intern build test. Turns 138 AMI meeting transcripts into a memory store, answers
questions with word-for-word citations, and writes a pre-meeting brief.

Python 3.9+, standard library only. Nothing to install.

## Setup

1. Unzip the data so that `data/transcripts/*.txt` and `data/questions.json` exist.
2. Copy `.env.example` to `.env` and add one key:
   - `GEMINI_API_KEY=...` (free at https://aistudio.google.com/apikey), or
   - `GROQ_API_KEY=...` (free at https://console.groq.com/keys), or
   - `LLM_PROVIDER=ollama` for a local model.
3. `python main.py models` lists the model ids your key can use. Set `LLM_MODEL=` in `.env` if the
   default is not in the list.

Optional, for search by meaning: install [Ollama](https://ollama.com), run `ollama pull nomic-embed-text`,
then `python main.py embed` (about 12 minutes for this corpus, offline). Without it, search is keyword-only.

## Run

```
python main.py build          # ingest (code) + extract (model) + reconcile (model). Resumable.
python main.py answer-all     # 25 questions + brief -> answers.json, then re-checks every citation
python main.py eval           # my 14 questions -> runs/eval_report.md
python main.py stats          # measured tokens and time, scaled to 1,000 meetings
```

## Adding meetings later

Three ways, all doing the same thing: check the file, store and index it (code), read it into the ledger
(1 to 4 model calls), and re-link the decision threads of the one team it belongs to (1 model call).
Nothing else in the store is touched, so the cost of adding a meeting does not grow with the corpus.

```
python main.py add path\to\TS3012e.txt                       # id, team and order read from the file or its name
python main.py add "Friday sync.txt" --team ACME7 --number 4  # when the file does not say
python main.py add *.txt --check                             # only check, change nothing
python main.py add TS3012c.txt --replace                     # load a corrected version of a stored meeting
```

- **Web UI:** the "Add meeting" tab does the same with a file picker and shows progress.
- **Folder drop:** copy files into `data/transcripts` and run `python main.py build`. It only processes
  what is new, so it can run on a schedule.

A transcript is plain text: each turn is a line `HH:MM:SS Speaker X` (one capital letter) followed by
its words. Optional header lines `Meeting:`, `Team:` and `Meeting number:` say what it is; without them
the id `TS3012c` is read as team TS3012, third meeting. The meeting number is what gives time order, and
so what lets a later decision replace an earlier one; two meetings of a team cannot share a number. A
new team name just works: questions that mention it are routed to it.

## Using a different data set

Nothing is tied to the AMI meetings. To start a separate memory from another set of transcripts:

```
set MEETMEM_DATA=D:\other\data        # a folder containing transcripts\*.txt (and optionally questions.json)
set MEETMEM_DB=D:\other\memory.db
python main.py build
python main.py serve
```

Or keep one memory and add the new files to it with `add` or the folder drop above. Team names are
read from the store, so questions, the timeline and search work for whatever teams are in it. An open
web page checks the store every few seconds and redraws when it changes. `answer-all` needs a
`questions.json` in the same shape as the supplied one; without it the rest still works.

## Web UI

```
python main.py serve          # then open http://127.0.0.1:8000
```

- **Ask:** type a question and watch the agent's steps stream in, then the answer with its citations.
- **Decision timeline:** pick a team, see each subject across meetings a to d with replaced items struck
  through, and rewind with the "as of" buttons.
- **Search:** raw retrieval, no answering model (keyword, plus meaning if `embed` has been run).
- **Answers & brief:** `answers.json` with every citation re-checked.
- **Add meeting:** upload a new transcript; it is checked before anything is stored.

Click any citation or ledger item to read it in the transcript, with the quoted words highlighted.

## Notes on running

The default model is `gemini-3.5-flash-lite`, falling back to `gemini-3.1-flash-lite` when its daily free
quota runs out. `LLM_MODEL` takes a comma-separated list. `extract --team TS3012 --redo` re-extracts one
team, for example with a stronger model.

Each answer gets one review call from a second reader. `REVIEW_MODEL` sets its model list; by default it
tries the stronger Gemini models first (20 free requests a day each) and ends on flash-lite.

The submitted `answers.json` came from two passes and a judge:

```
python main.py answer-all                      # pass 1
copy runs\answers_cache.json runs\answers_cache_v1.json
python main.py answer-all --redo               # pass 2
python main.py adjudicate                      # judge picks one draft per question -> answers.json
```

The judge's picks and reasons are in `runs/adjudication.json`. Delete that file and run `adjudicate`
again to re-judge, for example once the stronger models' quota has reset.

`build` and `answer-all` save after every model call. If a free-tier limit stops a run, run the same
command again and it carries on from where it stopped.

Ask anything, including new questions live:

```
python main.py ask "What did Team TS3005 decide about the scroll wheel?"
python main.py timeline TS3012 --topic "energy source"
python main.py timeline TS3012 --as-of c      # the record as it stood before the final meeting
python main.py search "LCD cost" --team IS1000   # raw retrieval, no model
python tests/test_offline.py                  # everything that needs no model
```

## How it works

```
transcripts ──code──> turns + BM25 index (SQLite FTS5)
     │
     └─model, once per meeting──> ledger items: decision / proposal / objection / action / fact
                                  each tied to a turn; speaker, time and quote verified by code
                                        │
            model, once per team────────┴──> threads + "new item supersedes old item" links
                                              (code rejects any link that runs backwards in time)

question ──code: ledger matches + thread history──> agent loop: search_memory / timeline /
             search_transcript / read_turns ──> draft answer
                 code checks every citation ──> second reader checks the draft ──> answer
```

| File | What it does | Model? |
| --- | --- | --- |
| `meetmem/store.py` | parse, chunk, index, search, time ordering | no |
| `meetmem/quotes.py` | find and verify exact quotes | no |
| `meetmem/extract.py` | read a meeting, write ledger items | yes, validated by code |
| `meetmem/reconcile.py` | threads, supersession, timeline view | yes, time rules in code |
| `meetmem/agent.py` | tool loop that answers a question | yes, citations checked by code |
| `meetmem/brief.py` | pre-meeting brief | yes, citations attached by code |
| `meetmem/evaluate.py` | scores the agent on `eval/my_questions.json` | no |
| `meetmem/llm.py` | one client for Gemini, Groq, Ollama; logs tokens and time | n/a |
| `meetmem/intake.py` | add one new transcript: check, store, extract, re-link its team | steps as above |
| `meetmem/server.py`, `web/index.html` | local web UI over the same functions | no |

## The decision timeline

The part I added beyond the brief. `python main.py timeline TEAM` shows each subject a team kept
returning to as one thread in time order, with every replaced position marked
`[SUPERSEDED by #id in meeting]`. It is `git log` for decisions: you can see what is current, what it
replaced, and who said each thing. `--as-of c` rewinds the record to any meeting, and the brief is built
from exactly that view, so the brief for the final meeting cannot leak what was said in it.

See `WRITEUP.md` for the design reasoning, results and failures.

Data: AMI Meeting Corpus, University of Edinburgh and partners, CC BY 4.0.
