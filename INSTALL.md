Install (developer/editable) instructions

This repository contains the Discord bot project. To make imports like `from db.mongo_adapters import ...` work from anywhere, install the project into your Python environment in editable mode.

PowerShell (recommended):

```powershell
# From the `DISCORD BOT` directory
cd 'F:\STARK-whiteout survival bot\DISCORD BOT'
# Use the virtual environment's pip if you have one, else system pip
pip install -e .
```

After installation the `db` package and other modules will be available on `sys.path` and imports will resolve regardless of working directory.

If you prefer not to install, run scripts from the `DISCORD BOT` folder or set `PYTHONPATH`:

```powershell
$env:PYTHONPATH = 'F:\STARK-whiteout survival bot\DISCORD BOT'
python .\main.py
```
