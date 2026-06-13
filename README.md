# codex-history-transfer

`codex-history-transfer` is a small offline tool for moving local Codex Desktop conversations between computers.

It packages one Codex thread as a zip file, then imports it on another machine by restoring:

- the rollout JSONL under `.codex/sessions`
- the thread row in `.codex/sqlite/state_*.sqlite`
- the optional workspace folder
- the `model_provider` and `cwd` metadata needed by Codex Desktop history search

This is an unofficial tool. Codex local storage can change between releases, so always run `--dry-run` first and keep backups.

## Status

This is an early MVP for Windows-first local transfers.

Implemented:

- `list`: show local threads from the Codex SQLite state DB
- `export`: export one thread to a zip package
- `import`: import a package, patch metadata, and back up DB files first
- `verify`: compare SQLite and JSONL metadata for a thread
- desktop GUI: select conversations, export packages, inspect packages, dry-run import, then import

Not implemented yet:

- full multi-thread export
- cloud sync
- automatic conflict merging
- redaction of private data before sharing

## Requirements

- Python 3.10+
- Local Codex Desktop data in `%USERPROFILE%\.codex`

No third-party Python packages are required.

## Desktop GUI

Launch the GUI from the project folder:

```powershell
cd D:\Agent\codex-history-transfer
python .\codex_history_transfer_gui.py
```

On Windows, you can also double-click:

```text
run_gui.bat
```

The GUI has two tabs:

- Export: search local Codex conversations, select one, choose whether to include workspace files, then create a zip package.
- Import: open a transfer zip, inspect its manifest and package entries, run a dry-run, then import after confirmation.

After editable install, you can also run:

```powershell
codex-history-transfer-gui
```

## Quick Start

Run from the project folder:

```powershell
cd D:\Agent\codex-history-transfer
python .\codex_history_transfer.py list --limit 10
```

Find a thread by keyword:

```powershell
python .\codex_history_transfer.py list --query "通信网络理论"
```

Export one thread:

```powershell
python .\codex_history_transfer.py export `
  --id 019eb05a-0d6a-7320-be60-b82b2f45efe2 `
  --out D:\Agent\thread.zip
```

Export one thread and include its workspace:

```powershell
python .\codex_history_transfer.py export `
  --id 019eb05a-0d6a-7320-be60-b82b2f45efe2 `
  --out D:\Agent\thread-with-workspace.zip `
  --include-workspace
```

Preview an import on another computer:

```powershell
python .\codex_history_transfer.py import D:\Agent\thread.zip --dry-run
```

Import and adapt the provider to the target computer:

```powershell
python .\codex_history_transfer.py import D:\Agent\thread.zip --provider current
```

If the same thread id already exists and you intentionally want to update it:

```powershell
python .\codex_history_transfer.py import D:\Agent\thread.zip --provider current --overwrite
```

Verify one imported thread:

```powershell
python .\codex_history_transfer.py verify --id 019eb05a-0d6a-7320-be60-b82b2f45efe2
```

## Safety Model

`import` creates backups before touching the SQLite state database:

```text
state_5.sqlite.bak-codex-history-transfer-YYYYMMDD-HHMMSS
state_5.sqlite-wal.bak-codex-history-transfer-YYYYMMDD-HHMMSS
state_5.sqlite-shm.bak-codex-history-transfer-YYYYMMDD-HHMMSS
```

If an existing rollout JSONL is overwritten with `--overwrite`, that file is also backed up.

The tool does not copy or migrate:

- `auth.json`
- API keys
- ChatGPT tokens
- plugin credentials
- OS account secrets

## Notes

`--provider current` detects the most common provider in the target Codex SQLite database and writes that value into the imported JSONL and SQLite row. This is useful when a thread exported from one machine has `model_provider = openai`, while the target machine expects `custom`.

`--include-workspace` can create large zip files. By default it excludes:

```text
.git
node_modules
.venv
venv
__pycache__
.mypy_cache
.pytest_cache
```

Add more exclusions with repeated `--exclude` options.

## Packaging Later

This file already supports Python packaging via `pyproject.toml`:

```powershell
pip install -e .
codex-history-transfer list --limit 10
```

## License

MIT
