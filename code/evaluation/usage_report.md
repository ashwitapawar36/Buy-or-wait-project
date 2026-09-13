# Token usage and cost analysis

Status: NOT RUN — this is a placeholder, not a completed submission report.

No authenticated Gemini dataset run was performed in the development environment.
Real model calls, input/output/thinking tokens, cache provenance, and configured cost
estimates will be recorded by `python code/main.py` on the user's computer.
A complete run replaces this file. Sample/limited/failed runs keep separate reports
in their run directories and cannot overwrite a successful final report.

Set `GEMINI_INPUT_USD_PER_MILLION` and `GEMINI_OUTPUT_USD_PER_MILLION` in `.env`
for your actual model and tier. Do not claim zero cost unless your tier is free.
The packaging command requires a complete run and configured cost rates.
