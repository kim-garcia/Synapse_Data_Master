# Synapse Data Master — VinSolutions & COAT Audits

Browser automation that helps audit dealer accounts. You log in **manually**
in a real browser window; the script then walks each row of a CSV, reads values
from the web app, and writes the answers back into the CSV. It saves after every
row, so you can stop and re-run any time and it picks up where it left off.

There are two audits:

| Script | What it checks | Reads CSV column | Writes |
|---|---|---|---|
| `audit_automation.py` | VinSolutions dealer features | `MAPPING_PFA_ID` | several columns incl. `Vin Feature Enabled` |
| `coat_audit.py` | COAT business-operation status | `ROOFTOP_ACCOUNT_CAID` | `COAT status` |

---

## What's in the folder

- `audit_automation.py` — VinSolutions audit
- `coat_audit.py` — COAT audit
- `coat_test.py` — quick **single-account** COAT test (doesn't touch the CSV)
- `audit.csv` — the input **and** output file (results are written back here)
- `SFXvsFulfillmentAuditMapping.csv` — product → feature-code mapping used by the VinSolutions audit
- `requirements.txt` — Python dependencies
- `README.md` — this file

> Folders you can ignore / delete: `audit-env/` (the original machine's virtual
> environment — make your own, see below), `browser_profile/` and
> `coat_profile/` (saved logins), `__pycache__/`, `*_log.txt`. See **Security**.

---

## 1. Prerequisites

- **Python 3.10 or newer** (built and tested on 3.13).
  Get it from <https://www.python.org/downloads/> and, on Windows, tick
  **"Add python.exe to PATH"** during install.
- Internet access. Playwright downloads its own Chromium (next step) — you do
  **not** need Chrome installed.

Check Python is available:

```powershell
python --version
```

---

## 2. One-time setup

Open a terminal **in this folder** (the unzipped `audit` folder).

### Windows — PowerShell
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```
If activation is blocked with a script-execution error, run this once in the
same window and retry the activate line:
```powershell
Set-ExecutionPolicy -Scope Process RemoteSigned
```

### Windows — Command Prompt (cmd)
```bat
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
python -m playwright install chromium
```

### macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

You only do this once. After that, each time you open a new terminal just
**activate the environment** again (the `Activate` / `activate` line above).

---

## 3. Check your input file

`audit.csv` must contain the key column for the audit you want to run:

- VinSolutions audit → a **`MAPPING_PFA_ID`** column with the dealer IDs.
- COAT audit → a **`ROOFTOP_ACCOUNT_CAID`** column (values look like `CA11252814`).

Result columns are created automatically if they don't exist. Rows with an empty
key are skipped.

---

## 4. Run an audit

Make sure the virtual environment is **activated** first (you'll see `(.venv)`
at the start of the prompt).

### VinSolutions audit
```powershell
python audit_automation.py
```
1. A browser window opens on VinSolutions.
2. **Log in** in that window.
3. Switch back to the terminal, type **`yes`**, press Enter.
4. It processes each row, saving `audit.csv` after every one.

### COAT audit
```powershell
python coat_audit.py
```
1. A browser opens on the Microsoft sign-in page.
2. **Log in**; wait until the COAT search page appears.
3. Switch back to the terminal, type **`yes`**, press Enter.
4. It searches each `ROOFTOP_ACCOUNT_CAID` and writes the `COAT status` column.

### Quick single-account COAT test
Use this to verify one account end-to-end before running the whole file:
```powershell
python coat_test.py CA11252814
```
Same login flow; it prints the result and **leaves the browser open** so you can
inspect the page. Press Enter in the terminal to close it.

---

## How re-running / resuming works

- The CSV is saved **after every row**.
- A row is considered "done" when all of its result columns are filled, and is
  **skipped** on the next run.
- To force a re-check of a row, clear its result cell(s) in `audit.csv` and run
  again.
- If a run crashes or you close it, just run the same command again.

## Output

- Results are written **back into `audit.csv`** (new columns at the end).
- Step-by-step logs are written to `audit_log.txt` (VinSolutions) and
  `coat_log.txt` (COAT) — handy if something doesn't look right.

---

## Security — please read before sharing or first run

- **Logins are saved locally.** `browser_profile/` (VinSolutions) and
  `coat_profile/` (COAT) keep your signed-in session so you don't log in every
  run. **Never share these folders** — they can carry an active session. If they
  arrived inside the zip, **delete them before your first run** and log in with
  your own account.
- **`audit.csv` may contain business data.** Treat it as confidential.
- Don't commit `.venv/`, `browser_profile/`, `coat_profile/`, or `*_log.txt`
  (the included `.gitignore` already excludes the profiles and CSVs).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python` not found | Reinstall Python with "Add to PATH", or use `py` instead of `python`. |
| `ModuleNotFoundError: playwright` | Activate the venv, then `pip install -r requirements.txt`. |
| `Executable doesn't exist` / browser won't open | Run `python -m playwright install chromium`. |
| PowerShell won't activate the venv | `Set-ExecutionPolicy -Scope Process RemoteSigned`, then activate again. |
| "Not authenticated" / stops immediately | Finish logging in **in the browser**, then type `yes` (or re-run). |
| Wrong / missing CSV columns | The script logs the headers it actually found — check the delimiter and that the key column name matches. |

> Note: the COAT audit is still being tuned to the live site. Its selectors live
> in a clearly-marked block at the top of `coat_audit.py`; use `coat_test.py` to
> confirm one account and adjust there if the page layout differs.
