# Start here (Windows / VS Code)

1. Extract this ZIP into a NEW folder. Open the extracted project folder in VS Code.
2. Copy `.env.example` to `.env`. Paste your Gemini API key into `.env` only.
   Keep `GEMINI_MODEL=gemini-2.5-flash`, or use an available compatible Gemini model.
3. Set both token prices in `.env` to match your tier. If your requests are on a free
   tier, set both to `0`. Otherwise use Google's pricing page. Do not guess rates.
4. In the VS Code terminal, run these commands ONE AT A TIME:

```powershell
py --version
py code/main.py --check
py -m unittest discover -s code/tests -v
py code/main.py --samples --limit 1
```

Python 3.10 or newer is required. No package installation is needed. If you already
created `.venv`, you can replace `py` with `.\.venv\Scripts\python.exe` in commands.

The first two checks do not call Gemini. The last command does call Gemini.
A run prints its output folder under `runs/`; inspect its `predictions.csv` and audit JSON.

Then evaluate all public examples:

```powershell
py code/main.py --samples
```

Open `runs/<printed-run-id>/sample_scores.json` for field-by-field accuracy and actual
versus expected values. These public examples are tests, not hidden evaluation labels.

Then run the 250 evaluation requests:

```powershell
py code/main.py
```

Only a COMPLETE run writes root `output.csv` and the final usage report. The run
can take time because it reads financial evidence for each request. Progress prints
as it proceeds. Existing evidence cache is reused automatically after interruption.

Finally package:

```powershell
py code/package.py
```

Upload these three files from `submission/`:

- `code.zip`
- `output.csv`
- `chat_transcript.txt`

Submit at:
[HackerRank submission](https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission)

## Troubleshooting

- **401 / 403**: check your key and access in `.env`. Never send the key in a screenshot.
- **404**: set `GEMINI_MODEL` to a model available to your API key.
- **429**: rate/quota limit; completed evidence remains cached. Increase
  `GEMINI_MIN_INTERVAL_SECONDS` for request-rate limits. Exhausted daily quota requires
  quota availability, not repeated immediate retries.
- **Evidence validation error**: inspect the printed request ID and run audit/cache.
  A missing image amount or unsupported factual extraction is never replaced by zero.
  Retry a single request with `--request-id request_26 --refresh` after fixing the cause.
- **Missing exact exchange rate**: inspect the extracted currency/date. The program
  deliberately does not fetch live rates or invent a missing dated rate.
- **Cost rates not configured**: set the two rates and run again; cached results avoid
  another round of API calls. A regenerated report then enables packaging.

This ZIP contains source code and the original dataset. It does NOT contain completed
Gemini predictions or a measured sample score. Generate and review those locally.
