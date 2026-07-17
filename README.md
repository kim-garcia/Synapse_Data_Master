# Synapse Data Master

A data-QA system for the 3WM team that audits **billing vs. fulfillment** across
VinSolutions / COAT / Salesforce, and layers an **AI agent** on top to diagnose
gaps and recommend actions.

The repo has two parts:

```
Synapse_Data_Master/
├── scrapers/   ← browser automation that collects the audit data
└── synapse/    ← the AI agent + Streamlit web app
```

## `scrapers/` — data collection
Playwright scripts that log in (manually) and read status from each system,
writing results back into a CSV.

| Script | Audits |
|---|---|
| `audit_automation.py` | VinSolutions dealer features |
| `coat_audit.py` | COAT business-operation status |
| `sf_audit.py` | Salesforce asset status |
| `validate_audit.py` | validation / cross-checks |

Run them **from the repo root** so their data files resolve, e.g.:
```bash
python scrapers/audit_automation.py
```
See `scrapers/README.md` for full setup. Data files (`audit.csv`, the mapping
CSV, `browser_profile/`) live at the repo root and are **git-ignored**.

## `synapse/` — the AI agent + website
A Streamlit app where you ask in one sentence; a Gemini agent runs the audit,
computes exact over/under-billing figures (in Python, never by the LLM), and
writes a recommendation. Includes a fail-safe **demo mode** and built-in
**anonymization/masking** so no customer data reaches the AI.
```bash
cd synapse
python -m venv .venv && .venv/Scripts/activate   # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```
See `synapse/README.md` for details, including how each teammate sets their own
`GEMINI_API_KEY` (no key is stored in the repo).

## Security / privacy
- **No API keys in the repo.** Everyone uses their own `GEMINI_API_KEY`
  (environment variable or the app's sidebar field).
- **No customer data in the repo.** All `*.csv`, logs, and browser profiles are
  git-ignored. The app anonymizes/masks names, addresses, IDs, and product
  names before anything is shared or sent to the AI.
