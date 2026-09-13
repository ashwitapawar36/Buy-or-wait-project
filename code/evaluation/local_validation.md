# Local validation

28 offline unit and integration tests passed during development.
The original dataset contains 250 evaluation requests, 25 public examples, 275 profiles,
25,342 financial events, 215 messages, 790 payment offers, 134 rates, and 16 image links.
All original dataset file bytes were preserved and every referenced image is present.

The integration test uses a mocked Gemini response for the first public sample. It
checks the transport, cache, CSV, sample comparison, audit, and usage-report pipeline.
It is NOT a live Gemini evaluation or a measured model accuracy result.
No API key, completed final predictions, or invented usage report is included.
The submission packager was verified to reject a project without a complete dataset run.

Run again: `python -m unittest discover -s code/tests -v`.
