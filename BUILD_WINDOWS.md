# Build Windows Package

This project can be packaged as two standalone Windows executables with PyInstaller:

- `codex-history-transfer.exe`: console CLI
- `codex-history-transfer-gui.exe`: desktop GUI

## Requirements

- Windows
- Python 3.10+
- PyInstaller

Install PyInstaller:

```powershell
python -m pip install pyinstaller
```

## Build

From the repository root:

```powershell
python -m PyInstaller --noconfirm --clean --onefile --console --name codex-history-transfer .\codex_history_transfer.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --name codex-history-transfer-gui .\codex_history_transfer_gui.py
```

The executables will be written to:

```text
dist\
```

## Smoke Test

```powershell
.\dist\codex-history-transfer.exe --version
.\dist\codex-history-transfer.exe list --limit 1
.\dist\codex-history-transfer-gui.exe --smoke-test
```

The GUI executable is built without a console window, so `--smoke-test` exits silently when successful.
