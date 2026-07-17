"""
list_models.py — show which Gemini models YOUR key can use.

Run it when you get a 404 "model not found" or a quota error:

    $env:GEMINI_API_KEY="your_key"      # if not already set
    python list_models.py

Then set one of the printed names in the website's Model dropdown, or:
    $env:GEMINI_MODEL="<a name from the list>"
"""
import os
import sys

import google.genai as genai_pkg
from google import genai

print("google-genai version:", getattr(genai_pkg, "__version__", "unknown"), "\n")

key = os.environ.get("GEMINI_API_KEY") or (sys.argv[1] if len(sys.argv) > 1 else None)
if not key:
    sys.exit("Set GEMINI_API_KEY first, or pass the key as an argument: python list_models.py YOUR_KEY")

client = genai.Client(api_key=key)

print("All models on your key (name  ->  supported actions):\n")
any_found = False
for m in client.models.list():
    any_found = True
    name = m.name.replace("models/", "")
    # Try both SDK attribute names; don't crash if neither exists.
    methods = (getattr(m, "supported_actions", None)
               or getattr(m, "supported_generation_methods", None)
               or [])
    can_generate = ("generateContent" in methods) if methods else "?"
    flag = "  <-- usable" if can_generate is True else ""
    print(f"  - {name:45s} {methods}{flag}")

if not any_found:
    print("  (none — the key may be invalid, or the project needs billing/a tier)")
else:
    print("\nPick a name marked 'usable' (or any that mentions 'flash') for the app.")
