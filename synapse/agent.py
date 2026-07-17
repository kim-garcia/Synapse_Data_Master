"""
agent.py — the Synapse agent (Gemini function calling).

You type one sentence, e.g.:
    "Run the script on 100 dealers and tell me how many are over-billing,
     how much it adds up to, and what you recommend."

Gemini decides, on its own, to:
    1. call run_vin_audit(limit=100)   -> runs your real VinSolutions script
    2. call analyze_billing()          -> exact counts/dollars (computed in Python)
    3. write the answer + recommendation.

The dollar figures are ALWAYS computed in Python (tool #2). Gemini only phrases
them — it never does the math, so the numbers can't be hallucinated.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from synapse_core import analyze_billing, DEFAULT_MODEL, _client, _generate

HERE = Path(__file__).resolve().parent
AUDIT_CSV = HERE.parent / "audit.csv"
DEMO_CSV = HERE.parent / "audit_demo.csv"   # a copy, for hackathon/live demos
VIN_BATCH = HERE / "vin_batch.py"
STATUS_FILE = HERE / "run_status.json"

_SYSTEM = (
    "You are Synapse Data Master, a data-QA analyst for the 3WM team auditing "
    "billing vs. fulfillment in VinSolutions. When the user asks to run the audit, "
    "use run_vin_audit; then use analyze_billing to get the exact figures. NEVER "
    "invent numbers: quote them exactly as analyze_billing returns them. Answer in "
    "English, clear and stakeholder-friendly."
)


# ----------------------------------------------------------------------
# TOOL 1: run the real VinSolutions script as a subprocess, with progress
# ----------------------------------------------------------------------
def _demo_run(limit: int, csv_path: Path,
              progress: Callable[[dict], None] | None = None) -> dict:
    """Hackathon/demo path: no browser, no login. Simulate progress over a COPY."""
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    available = len(df)
    target = min(int(limit), available)
    for i in range(1, target + 1):
        if progress:
            progress({"state": "running", "processed": i, "target": target,
                      "message": f"[DEMO] Checking {i}/{target}…"})
        time.sleep(0.03)  # just enough to look live
    if progress:
        progress({"state": "done", "processed": target, "target": target,
                  "message": f"[DEMO] Done: {target} dealers (copy)."})
    return {"state": "done", "processed": target, "message": "demo",
            "error": None, "demo": True}


def run_vin_audit(limit: int, progress: Callable[[dict], None] | None = None,
                  demo: bool = False, csv_path: Path | None = None) -> dict:
    """Launch vin_batch.py and wait for it, reporting progress via `progress`.

    demo=True skips the browser entirely and simulates the run over a copy CSV."""
    if demo:
        return _demo_run(limit, csv_path or DEMO_CSV, progress)
    if STATUS_FILE.exists():
        STATUS_FILE.unlink()  # start clean so we don't read a stale status
    proc = subprocess.Popen([sys.executable, str(VIN_BATCH), "--limit", str(int(limit))])

    last = None
    while True:
        status = {}
        if STATUS_FILE.exists():
            try:
                status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                status = {}
        if status and status != last and progress:
            progress(status)
            last = status
        if status.get("state") in ("done", "error"):
            break
        if proc.poll() is not None and not status:
            break  # process died before writing any status
        time.sleep(1)

    proc.wait()
    final = {}
    if STATUS_FILE.exists():
        final = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    return {
        "state": final.get("state", "unknown"),
        "processed": final.get("processed", 0),
        "message": final.get("message", ""),
        "error": final.get("error"),
    }


# ----------------------------------------------------------------------
# TOOL 2: exact billing analysis (Python does the math)
# ----------------------------------------------------------------------
def analyze_billing_tool(csv_path: Path | None = None,
                         head: int | None = None) -> dict:
    path = csv_path or AUDIT_CSV
    if not Path(path).exists():
        return {"error": f"CSV not found: {path}"}
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return analyze_billing(df, only_processed=True, head=head)


# ----------------------------------------------------------------------
# The Gemini function-calling loop
# ----------------------------------------------------------------------
def _tool_declarations():
    from google.genai import types
    return types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="run_vin_audit",
            description="Runs the real VinSolutions audit script on the first N "
                        "pending dealers. Requires a browser login.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"limit": types.Schema(
                    type=types.Type.INTEGER,
                    description="how many dealers to process (e.g. 100)")},
                required=["limit"],
            ),
        ),
        types.FunctionDeclaration(
            name="analyze_billing",
            description="Returns EXACT over/under-billing counts and dollar amounts "
                        "for the already-verified dealers (confirmed vs. justified).",
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
    ])


def run_agent(user_message: str, api_key: str | None = None,
              model: str = DEFAULT_MODEL,
              progress: Callable[[dict], None] | None = None,
              demo: bool = False, csv_path: Path | None = None) -> str:
    """Drive Gemini through the tools and return its final answer.

    demo=True runs on a copy CSV with no browser/login (safe for live demos)."""
    from google.genai import types

    active_csv = csv_path or (DEMO_CSV if demo else AUDIT_CSV)
    client = _client(api_key)
    tools = _tool_declarations()
    config = types.GenerateContentConfig(
        tools=[tools],
        system_instruction=_SYSTEM,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents = [types.Content(role="user", parts=[types.Part(text=user_message)])]
    demo_limit = None  # in demo mode, analyze exactly what was "run"

    for _ in range(6):  # a small ceiling so we never loop forever
        resp = _generate(client, model=model, contents=contents, config=config)
        cand = resp.candidates[0]
        parts = cand.content.parts or []
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

        if not calls:
            return (resp.text or "").strip()

        contents.append(cand.content)  # keep the model's tool-request turn
        for call in calls:
            args = dict(call.args or {})
            if call.name == "run_vin_audit":
                limit = int(args.get("limit", 100))
                if demo:
                    demo_limit = limit
                result = run_vin_audit(limit, progress=progress,
                                       demo=demo, csv_path=active_csv)
            elif call.name == "analyze_billing":
                result = analyze_billing_tool(csv_path=active_csv, head=demo_limit)
            else:
                result = {"error": f"unknown tool: {call.name}"}
            contents.append(types.Content(
                role="user",
                parts=[types.Part.from_function_response(
                    name=call.name, response={"result": result})],
            ))

    return "I couldn't complete the task within the allowed number of steps."
