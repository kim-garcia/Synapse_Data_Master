# Synapse Data Master — AI layer + website

This adds the two missing pieces from the Synapse infographic to your existing
audit scrapers: **AI Diagnosis (step 5)**, **Report & Suggest (step 6)**, and a
small **website** that ties everything together. Your VinSolutions / COAT
scrapers and `audit.csv` stay exactly as they are — this reads their output.

## What's here

| File | What it is |
|---|---|
| `synapse_core.py` | The brains: anonymize → summarize → exact billing math → ask Gemini. No UI, no browser. |
| `diagnose.py` | **Phase 1** — run the AI diagnosis from the command line. Start here. |
| `vin_batch.py` | **Phase 3** — runs your `audit_automation.py` on the first *N* pending dealers. |
| `agent.py` | **Phase 3** — the Gemini function-calling agent (one sentence → run script → answer). |
| `app.py` | The website: **🤖 Agente** tab + **📊 Panel** tab. |
| `requirements.txt` | Python packages. |

## Setup (once)

From inside this `synapse/` folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Get a Gemini key at <https://aistudio.google.com/app/apikey>, then:

```powershell
$env:GEMINI_API_KEY="your_key_here"     # PowerShell
```

## Phase 1 — prove the AI works (no website)

```powershell
python diagnose.py
```

It reads `..\audit.csv`, hides every dealer name/address, prints the exact facts
it will send, then prints Gemini's diagnosis and recommended actions.

## Phase 2 — the website

```powershell
streamlit run app.py
```

Opens at <http://localhost:8501>. Upload a CSV (or it uses `..\audit.csv`),
click **Run diagnosis**, and use the question box.

## The one rule that matters: privacy

`audit.csv` holds real dealer names and addresses. **Those never go to Gemini.**
`anonymize()` drops the name/address columns and swaps account IDs for surrogate
tags like `D01234`. A local lookup keeps the mapping so *you* can trace a finding
back to the real dealer — it never leaves your machine. This is step 2
("Cleanse & Anonymize") of your own diagram.

## Phase 3 — the agent (already built, for VinSolutions)

Open the website and go to the **🤖 Agente** tab. Type a sentence like:

> Corre el script en 100 dealers y dime cuántos son over-billing, a cuánto
> asciende y qué recomiendas.

What happens:

1. Gemini calls `run_vin_audit(limit=100)` → runs your real `audit_automation.py`
   on the first 100 pending dealers (via `vin_batch.py`).
2. A browser window opens — **log in to VinSolutions once**; the agent detects
   the login and continues on its own (your session is saved for next time).
3. Gemini calls `analyze_billing()` → **exact** over/under-billing counts and
   dollar totals, computed in Python (never by the model).
4. Gemini writes the answer + recommendation, and you can **download** the
   verified-dealers CSV and a Markdown report.

You can also run the batch alone, without the website:

```powershell
python vin_batch.py --limit 100
```

### Command line (no website) — quick check
```powershell
python vin_batch.py --limit 5      # process 5 dealers to confirm the flow
```

### Next extensions
- Add COAT (`coat_audit.py`) as a second agent tool the same way.
- Tune the "confirmed over-billing" rule with the team (currently: billed but the
  VinSolutions feature is **not** enabled).
- Schedule a daily run with n8n and email the Markdown report.
