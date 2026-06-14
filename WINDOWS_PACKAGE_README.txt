codex-history-transfer 1.0.2 for Windows

This is a portable Windows package. It does not install anything globally.

Quick start:

1. Unzip this folder.
2. Run codex-history-transfer-gui.exe.
3. Use Export to select a local Codex Desktop conversation and create a transfer zip.
4. On another computer, run the same GUI, open the transfer zip in Import, run Dry run, then Import.

Included files:

- codex-history-transfer-gui.exe  desktop GUI
- codex-history-transfer.exe      command-line tool
- README.md                       full project documentation
- RELEASE_NOTES.md                release notes
- LICENSE                         license

Safety notes:

- The tool works locally. It does not upload conversations.
- Import creates backups before changing the Codex SQLite state database.
- The tool does not migrate auth.json, API keys, ChatGPT tokens, or plugin credentials.

Project:

https://github.com/abigfly/codex-history-transfer
