"""
Synapse Data Master — the website.

Two tabs:
  🤖 Agent      — type a sentence; it runs your VinSolutions script, then reports
                  exact over/under-billing numbers and a recommendation.
  📊 Dashboard  — upload/preview a CSV, charts, AI diagnosis, and free Q&A.

Run it:
    pip install -r requirements.txt
    streamlit run app.py
"""

import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from synapse_core import (
    anonymize, build_gap_summary, summary_to_text, diagnose, answer_question,
    analyze_billing, analysis_to_text, available_models, DEFAULT_MODEL, PROCESSED_MARKER,
    mask_dataframe,
)
from agent import run_agent

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "audit.csv"
DEMO_CSV = Path(__file__).resolve().parent.parent / "audit_demo.csv"

st.set_page_config(page_title="Synapse Data Master", page_icon="🤖", layout="wide")
st.title("🤖 Synapse Data Master")
st.caption("Automated · Intelligent · Secure — VinSolutions billing-vs-fulfillment QA")

with st.sidebar:
    st.header("Gemini")
    api_key = st.text_input(
        "API key", type="password",
        help="Paste your Google AI Studio key, or set GEMINI_API_KEY and leave this blank.",
    ) or None

    # Model picker — lists exactly what your key supports (avoids 404/quota errors).
    models = available_models(api_key)
    if models:
        default_idx = models.index(DEFAULT_MODEL) if DEFAULT_MODEL in models else 0
        model = st.selectbox("Model", models, index=default_idx,
                             help="These are the models your key can use.")
    else:
        model = st.text_input("Model", value=DEFAULT_MODEL,
                              help="Couldn't list models (check your key). Type one manually.")
    st.divider()
    st.header("Demo mode 🎬")
    demo = st.toggle("Use the copy (no browser)", value=True,
                     help="Recommended for demos: runs on audit_demo.csv, with no login "
                          "or live scraping. Fast and fail-safe.")
    if st.button("Create / update demo copy"):
        if DEFAULT_CSV.exists():
            raw = pd.read_csv(DEFAULT_CSV, dtype=str, keep_default_na=False)
            # Readable-masked demo: names/addresses gone, CAID -> CA11212XXX,
            # products -> category codes like TXT-042.
            masked, _ = mask_dataframe(raw)
            masked.to_csv(DEMO_CSV, index=False)
            st.success(f"Masked demo copy created: {DEMO_CSV.name} "
                       "(no names; CAIDs masked; products as category codes).")
        else:
            st.error("Can't find audit.csv to copy.")
    st.divider()
    st.caption("🔒 Dealer names and addresses are removed before any data is sent to Gemini.")

# Which file the app reads/analyzes depends on the demo toggle.
ACTIVE_CSV = DEMO_CSV if demo else DEFAULT_CSV

tab_agent, tab_panel = st.tabs(["🤖 Agent", "📊 Dashboard"])

# ======================================================================
# TAB 1 — THE AGENT
# ======================================================================
with tab_agent:
    st.subheader("Ask in one sentence")
    if demo:
        st.success("**Demo mode on** 🎬 — runs on the copy, with no browser or login. "
                   "Instant and fail-safe for presenting.", icon="🎬")
        if not DEMO_CSV.exists():
            st.warning("The copy doesn't exist yet. Click **Create / update demo copy** "
                       "in the sidebar.", icon="⚠️")
    else:
        st.info("On run, a browser window opens. **Log in to VinSolutions** there; "
                "the agent detects the login and continues on its own.", icon="🔑")

    prompt = st.text_area(
        "Instruction",
        value="Run the script on 100 dealers and tell me how many are over-billing, "
              "how much money it adds up to, and what you recommend.",
        height=90,
    )

    if st.button("▶ Run agent", type="primary"):
        bar = st.progress(0, text="Starting…")
        status_line = st.empty()

        def on_progress(status: dict):
            state = status.get("state", "")
            msg = status.get("message", state)
            done, target = status.get("processed", 0), status.get("target", 0) or 1
            if state == "running":
                bar.progress(min(done / target, 1.0), text=msg)
            else:
                status_line.write(f"⏳ {msg}")

        try:
            with st.spinner("The agent is working…"):
                answer = run_agent(prompt, api_key=api_key, model=model,
                                   progress=on_progress, demo=demo, csv_path=ACTIVE_CSV)
            bar.progress(1.0, text="Done")
            st.session_state["agent_answer"] = answer
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                st.warning(
                    f"⚠️ Daily free quota reached for **{model}** "
                    "(the free tier is limited requests/day per model).\n\n"
                    "**Fix:** pick a different model in the sidebar dropdown — each model "
                    "has its own daily quota. Try **gemini-2.5-flash-lite** or "
                    "**gemini-2.0-flash-lite** (bigger free limits). It resets every 24h.",
                    icon="⚠️")
            else:
                st.error(msg)

    if st.session_state.get("agent_answer"):
        st.markdown("### Agent response")
        st.markdown(st.session_state["agent_answer"])

        # Deterministic table + export of exactly what was verified.
        if ACTIVE_CSV.exists():
            df = pd.read_csv(ACTIVE_CSV, dtype=str, keep_default_na=False)
            # In demo mode, scope the table to the number requested in the prompt,
            # so it matches what the agent "ran".
            m = re.search(r"\d+", prompt) if demo else None
            head = int(m.group()) if m else None
            analysis = analyze_billing(df, only_processed=True, head=head)
            st.markdown("### Exact numbers")
            st.code(analysis_to_text(analysis))

            if head is not None:
                verified = df.head(head)
            elif PROCESSED_MARKER in df.columns:
                verified = df[df[PROCESSED_MARKER].astype(str).str.strip() != ""]
            else:
                verified = df

            # Masked export: ONLY Deal ID, Name, Price, Category — nothing real.
            # (demo data is already masked; real data gets masked on the fly)
            exp = verified if demo else mask_dataframe(verified)[0]
            blank = pd.Series([""] * len(exp))
            export_df = pd.DataFrame({
                "Deal ID": exp.get("ROOFTOP_ACCOUNT_CAID", blank).values,
                "Nombre": exp.get("PRODUCT_NAME", blank).values,
                "Precio": exp.get("ASSET_TOTAL_PRICE", blank).values,
                "Categoria": exp.get("GAP_TYPE", blank).values,
            })
            stamp = datetime.now().strftime("%Y%m%d_%H%M")
            c1, c2 = st.columns(2)
            c1.download_button(
                "⬇ Download (CSV — anonymized: Deal ID, Nombre, Precio, Categoria)",
                export_df.to_csv(index=False).encode("utf-8"),
                file_name=f"synapse_export_{stamp}.csv", mime="text/csv")
            report = (f"# Synapse Data Master — Report {stamp}\n\n"
                      f"## Exact numbers\n\n{analysis_to_text(analysis)}\n\n"
                      f"## Agent diagnosis\n\n{st.session_state['agent_answer']}\n")
            c2.download_button(
                "⬇ Download report (Markdown)",
                report.encode("utf-8"),
                file_name=f"synapse_report_{stamp}.md", mime="text/markdown")

# ======================================================================
# TAB 2 — THE DASHBOARD (upload / charts / diagnosis / Q&A)
# ======================================================================
with tab_panel:
    upload = st.file_uploader("Upload an audit CSV", type="csv")
    if upload is not None:
        df = pd.read_csv(upload, dtype=str, keep_default_na=False)
    elif ACTIVE_CSV.exists():
        df = pd.read_csv(ACTIVE_CSV, dtype=str, keep_default_na=False)
    else:
        st.info("Upload a CSV to begin.")
        st.stop()

    safe_df, lookup = anonymize(df)
    summary = build_gap_summary(safe_df)
    gap = summary.get("gap_type", {})

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows", f"{summary['total_rows']:,}")
    c2.metric("Under-billing", f"{gap.get('Under Billing', 0):,}")
    c3.metric("Over-billing", f"{gap.get('Over Billing', 0):,}")
    c4.metric("Dealers hidden", f"{len(lookup):,}")

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Gaps at a glance")
        if gap:
            st.bar_chart(pd.Series(gap, name="rows"))
        top = summary.get("top_products_by_gap_count", {})
        if top:
            st.caption("Top products by gap count")
            st.bar_chart(pd.Series(top, name="rows"))
        with st.expander("Exact figures sent to Gemini (no names)"):
            st.code(summary_to_text(summary))

    with right:
        st.subheader("AI diagnosis")
        if st.button("Generate diagnosis"):
            with st.spinner("Gemini is analyzing…"):
                try:
                    st.session_state["report"] = diagnose(summary, api_key=api_key, model=model)
                except Exception as e:
                    st.error(str(e))
        if st.session_state.get("report"):
            st.markdown(st.session_state["report"])

        st.subheader("Free question")
        q = st.text_input("e.g. Which products drive the most over-billing?")
        if q:
            with st.spinner("Thinking…"):
                try:
                    st.markdown(answer_question(q, summary, api_key=api_key, model=model))
                except Exception as e:
                    st.error(str(e))
