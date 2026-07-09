# Repository Instructions

- Read `ARCHITECTURE.md` before starting any work.
- Follow the architecture in `ARCHITECTURE.md`.
- Keep all Markdown files in sync with code and behavior changes; update them whenever they become stale.
- Avoid duplicating the same behavioral details across Markdown files; keep one canonical location per topic and let other docs reference it briefly.
- Keep top-of-file source comments aligned with `ARCHITECTURE.md`; they should briefly describe each file's role without becoming a second source of behavioral truth.
- Run the app locally from the repository checkout through the shared `.venv` and Homebrew-managed services; do not add Docker-based runtime paths.
- Keep the app simple and avoid adding framework dependencies.
- Keep secrets in `.secrets`; never commit or print them.
- Keep summarization behavior centralized in `SUMMARIZER.md`.
- Prefer constants over adding new configuration parameters unless the user explicitly asks for configurability.
- Do not use the official YouTube captions API for caption downloads because it does not reliably work for non-owned videos; prefer non-official caption retrieval or local audio transcription instead.
- Do not add automated tests unless project requirements change.
- Use manual verification with `python3 -m py_compile texttube_app.py` and `bash -n texttube`.
- Before pushing anything to a remote, test everything locally, including the relevant checks in the separate `../homebrew-texttube` formula repository.
- For end-to-end subscription checks, an hourly automation run with verbose output redirected to a timestamped `var/logs/automation-runs/` file is an easy low-context test path.
- Do not use numbered lists in Markdown files.
