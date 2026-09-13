# Buy or Wait?

A Python financial decision agent developed from the HackerRank Orchestrate September
2026 starter challenge. It reconstructs commitments from CSVs, messages and images,
forecasts 90 days of cash flow, and verifies payment plans before recommending them.

**Start with [QUICKSTART.md](QUICKSTART.md).** Python 3.10+; no third-party packages.
Your API key stays in `.env` and is sent only to Google's Gemini API over HTTPS.
The supplied user financial records and relevant images are sent to Gemini for interpretation.

## What is implemented

- CSV joins and validation across all provided files, including linked image amounts.
- Gemini text/image interpretation with JSON schema, source references and local validation.
- Explicit distinction between cash, pending credits, settled history, and investments.
- Recurring expense inference and confirmed income streams, including amendments.
- Decimal currency calculations and exact dated exchange rates.
- Full/partial/installment/wait planning, preference filtering and deterministic ranking.
- Protected-expense enforcement and combinations of up to three permitted spending changes.
- Date-by-date safety checks and audit traces with evidence and forecast source IDs.
- Real API token usage, thinking tokens, local-cache provenance and configurable cost estimates.
- Public-sample evaluation with no sample labels in model inputs.
- Submission packaging that checks complete output, input/output hashes and the usage report.

## Architecture

| Component | Responsibility |
|---|---|
| `code/buy_or_wait/data.py` | Read-only datasets, input validation, money/date handling |
| `code/buy_or_wait/evidence.py` | Gemini REST calls, retries, schema checks, cache and source validation |
| `code/buy_or_wait/evidence_prompt.txt` | Versioned financial extraction instructions |
| `code/buy_or_wait/forecast.py` | Recurrence, amendments, explicit debits and cash-flow simulation |
| `code/buy_or_wait/planner.py` | Candidate schedules, ranking, final CSV verification |
| `code/buy_or_wait/reporting.py` | Sample comparisons and actual usage accounting |
| `code/main.py` | Run orchestration, atomic outputs, diagnostics and audit files |
| `code/package.py` | Generate the three final submission artifacts |
| `code/tests/` | Offline synthetic safety and integration tests |

The model extracts facts, not final payment recommendations. Its output cannot change
structured payment preferences, protected categories, supplied installment schedules,
or the minimum balance. Monetary/date references are validated before calculation.
Source validation is not a proof that an LLM interpreted the underlying evidence correctly;
audit the examples and investigate mismatches before submitting.

## Run commands

```bash
python code/main.py --check
python -m unittest discover -s code/tests -v
python code/main.py --samples --limit 1
python code/main.py --samples
python code/main.py
python code/package.py
```

Other useful options:

```bash
python code/main.py --request-id request_26
python code/main.py --request-id request_26 --refresh
python code/main.py --offline
python code/main.py --samples --expense-estimator recent-median
```

`--offline` uses previously validated evidence only; it is not an approximation that
ignores messages/images. `--refresh` changes evidence and causes fresh API charges.
Limited and sample runs never replace root `output.csv`. A failed full run preserves
any earlier final output and its matching report, while saving diagnostics separately.

## Financial assumptions that remain configurable or approximate

The task does not specify an exact statistical estimator for recurring variable
expenses or an intraday ordering convention. These are explicit implementation choices:

- Forecast includes request_date through request_date + 90 days, inclusive.
- Current balance already includes settled records on or before request_date.
- Within each day, existing debits run before confirmed credits; the requested payment
  is made after those credits. Any earlier intraday dip below the minimum fails safety.
- Historical monthly expense groups use category + description + currency. Variable
  groceries/transport/dining group by category + currency because merchants can change.
- At least three distinct observed dates support a recurrence. Monthly groups need
  consecutive monthly observations; other periods use median observed spacing and a
  regularity threshold. Old discontinued patterns are not automatically extrapolated.
- The default variable amount is max(latest amount, median of latest three observations).
  `recent-median` uses the latest-three median. Neither is guaranteed to reproduce an
  unpublished ground-truth estimator. The run records which estimator was used.
- Each recurring expense is represented by its latest historical event ID for spending
  changes. Reductions use its supplied minimum_allowed_amount; other intermediate
  reductions are not searched. This makes the search finite and auditable.
- Installment count is capped by max_installment_months and calendar duration is checked.
  Every actual payment follows the supplied day interval, including 28/30/31-day offers.
- A one-cent discrepancy between rounded installments and the offer total is tolerated.
- Missing dated rates cause a visible error; no live rates or guessed conversions are used.

These assumptions and model extraction can affect sample/hidden accuracy. This is a
runnable implementation with local tests, not a claim of perfect challenge accuracy.
No live Gemini run or final evaluation output was produced while building this ZIP.

## Ranking

Only plans completed by the request deadline and safe across the horizon are eligible.
Prefer: no spending changes, lower total payment, earlier start, fewer payments, then
lowest numeric payment option ID. Full-payment capacity is computed independently of
payment preferences. Partial payment is exactly the required two-payment schedule.

## API usage and caching

One extraction is normally needed per request; a schema repair can make a second call.
Transient HTTP failures use bounded backoff. Content-hashed local caches include the
model, instructions, input records, and image bytes. Cache hits make no network call.
Different expense estimators reuse the same evidence cache.

`runs/<id>/api_calls.jsonl` records returned usage metadata. `usage_report.md` reports
current-run tokens and previously cached origin tokens separately; original cached
extraction is not charged a second time. Thought tokens are counted as output tokens.
Model prices are explicit `.env` configuration, not an invented billing estimate.

Gemini references:
[structured output](https://ai.google.dev/gemini-api/docs/structured-output),
[image understanding](https://ai.google.dev/gemini-api/docs/image-understanding),
[API pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Submission and attribution

The supplied challenge statement, AGENTS.md and dataset are preserved from the uploaded
HackerRank starter repository. No new license or redistribution permission is claimed
for those supplied materials. The implementation lives under `code/`.

Continue recording actual development turns in root `log.txt` per AGENTS.md. It contains
conversation summaries, not a fabricated full verbatim assistant transcript. It is
excluded from Git and copied as `chat_transcript.txt` for submission. Keep secrets out.

After the successful full run, `python code/package.py` produces:

- `submission/code.zip`: runnable project, prompts/configuration, original dataset,
  cached evidence, and `evaluation/usage_report.md` at the ZIP root.
- `submission/output.csv`: exactly one prediction for each evaluation request.
- `submission/chat_transcript.txt`: the actual development log.

[Submit to HackerRank](https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission).
