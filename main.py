from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import pandas as pd
import numpy as np
import json, io, os, re
from typing import Any, Optional

app = FastAPI(title="NordSheet API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*nordsheet\.com|https://nsh-frontend.*\.vercel\.app|http://localhost:3000",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════

def read_upload_bytes(b: bytes, filename: str) -> pd.DataFrame:
    ext = os.path.splitext(filename or "")[1].lower()
    na_vals = ["N/A","NA","n/a","na","NULL","null","None","none","NaN","nan","-","--",""]
    if ext == ".csv":
        return pd.read_csv(io.BytesIO(b), keep_default_na=True, na_values=na_vals)
    elif ext in {".xlsx", ".xls"}:
        return pd.read_excel(io.BytesIO(b), keep_default_na=True, na_values=na_vals)
    raise ValueError("Endast CSV, XLSX och XLS stöds.")


def suggest_columns(cols: list) -> dict:
    cols_lower = {c: c.lower() for c in cols}
    keywords = {
        "period":       ["period","month","månad","date","datum","year","år"],
        "account":      ["account","konto","kontonr","account_id","gl"],
        "account_name": ["account_name","kontonamn","name","description","benämning"],
        "actual":       ["actual","utfall","fact","verklig","bokfört","belopp"],
        "budget":       ["budget","plan","planned","bud"],
        "entity":       ["entity","bolag","company","legal","enhet"],
        "cost_center":  ["cost_center","costcenter","cc","kostnadsställe","resultatenhet"],
        "project":      ["project","projekt"],
    }
    return {
        field: [c for c, cl in cols_lower.items() if any(kw in cl for kw in kws)]
        for field, kws in keywords.items()
    }


def fmt_sek(n: float, decimals: int = 0) -> str:
    abs_n = abs(n)
    if abs_n >= 1_000_000:
        return f"{n/1_000_000:.{decimals}f} MSEK"
    if abs_n >= 1_000:
        return f"{n/1_000:.{decimals}f} tkr"
    return f"{n:.{decimals}f} SEK"


def safe_pct(a: float, b: float) -> Optional[float]:
    if b and b != 0:
        return (a - b) / abs(b)
    return None


# ═══════════════════════════════════════════════════════════════════
# PACK COMPUTATION
# ═══════════════════════════════════════════════════════════════════

def compute_pack(df: pd.DataFrame, mapping: dict) -> dict:
    rename = {v: k for k, v in mapping.items() if v and v in df.columns}
    df = df.rename(columns=rename)
    warnings = []

    # Numeric coercion
    for col in ["actual", "budget"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Periods
    periods = []
    if "period" in df.columns:
        periods = sorted(df["period"].dropna().astype(str).unique().tolist())
    current_period  = periods[-1] if periods else "Unknown"
    previous_period = periods[-2] if len(periods) >= 2 else None

    total_actual = float(df["actual"].sum()) if "actual" in df.columns else 0
    total_budget = float(df["budget"].sum()) if "budget" in df.columns else 0

    top_budget, top_mom = [], []
    kpi_summary = {}
    by_account = pd.DataFrame()

    if "account" in df.columns and "actual" in df.columns:
        name_col = "account_name" if "account_name" in df.columns else None

        agg_cols = {"actual": "sum"}
        if "budget" in df.columns:
            agg_cols["budget"] = "sum"

        grp_cols = ["account"]
        if name_col:
            grp_cols.append(name_col)

        by_account = df.groupby(grp_cols, dropna=False).agg(agg_cols).reset_index()

        if "budget" in by_account.columns:
            by_account["variance"]     = by_account["actual"] - by_account["budget"]
            by_account["variance_pct"] = by_account.apply(
                lambda r: safe_pct(r["actual"], r["budget"]), axis=1
            )
        else:
            by_account["variance"]     = 0
            by_account["variance_pct"] = None
            warnings.append("Budgetkolumn saknas — variansanalys ej möjlig.")

        def row_to_dict(r):
            label = str(r.get("account_name") or r.get("account") or "—")
            return {
                "Konto":         str(r.get("account", "—")),
                "Label":         label,
                "Utfall":        round(float(r["actual"]), 0),
                "Budget":        round(float(r.get("budget", 0)), 0),
                "Vs budget diff": round(float(r.get("variance", 0)), 0),
                "Vs budget %":   round(float(r.get("variance_pct") or 0), 4),
            }

        top_budget = [row_to_dict(r) for _, r in by_account.sort_values("variance").head(10).iterrows()]
        top_mom    = [row_to_dict(r) for _, r in by_account.sort_values("variance", ascending=False).head(10).iterrows()]

        # MoM: compare current vs previous period if both exist
        mom_diff, mom_pct = None, None
        if previous_period and "period" in df.columns:
            cur_total  = float(df[df["period"].astype(str) == current_period]["actual"].sum())
            prev_total = float(df[df["period"].astype(str) == previous_period]["actual"].sum())
            mom_diff   = cur_total - prev_total
            mom_pct    = safe_pct(cur_total, prev_total)

        kpi_summary = {
            "Nu":             round(total_actual, 0),
            "Föregående":     round(float(df[df["period"].astype(str) == previous_period]["actual"].sum()), 0) if previous_period and "period" in df.columns else None,
            "MoM diff":       round(mom_diff, 0) if mom_diff is not None else None,
            "MoM %":          round(mom_pct, 4) if mom_pct is not None else None,
            "Budget":         round(total_budget, 0),
            "Vs budget diff": round(total_actual - total_budget, 0),
            "Vs budget %":    round(safe_pct(total_actual, total_budget) or 0, 4),
        }

    # Period-level time series (for forecast charts)
    period_series = []
    if "period" in df.columns and "actual" in df.columns:
        ps = df.groupby("period").agg(
            actual=("actual", "sum"),
            **({"budget": ("budget", "sum")} if "budget" in df.columns else {})
        ).reset_index().sort_values("period")
        period_series = ps.fillna(0).to_dict(orient="records")

    # Narrative
    var = total_actual - total_budget
    var_pct = safe_pct(total_actual, total_budget)
    narrative = (
        f"Period {current_period}: Utfall {fmt_sek(total_actual)} "
        f"mot budget {fmt_sek(total_budget)} "
        f"(avvikelse {fmt_sek(var, 0)}"
        f"{f', {var_pct*100:+.1f}%' if var_pct is not None else ''})."
    )
    if previous_period and kpi_summary.get("MoM diff") is not None:
        narrative += (
            f" Jämfört med {previous_period}: "
            f"{fmt_sek(kpi_summary['MoM diff'])}"
            f" ({kpi_summary.get('MoM %', 0)*100:+.1f}%)."
        )

    return {
        "current_period":  current_period,
        "previous_period": previous_period,
        "periods":         periods,
        "warnings":        warnings,
        "narrative":       narrative,
        "top_budget":      top_budget,
        "top_mom":         top_mom,
        "kpi_summary":     [kpi_summary],
        "total_actual":    round(total_actual, 0),
        "total_budget":    round(total_budget, 0),
        "period_series":   period_series,
        "account_rows":    by_account.fillna(0).to_dict(orient="records") if not by_account.empty else [],
    }


# ═══════════════════════════════════════════════════════════════════
# FORECAST ENGINE
# ═══════════════════════════════════════════════════════════════════

def build_forecast_data(pack: dict) -> dict:
    """
    Build forecast from pack data.
    Uses linear regression on period_series for trend,
    then extrapolates forward with confidence intervals.
    """
    period_series = pack.get("period_series", [])
    total_actual  = pack.get("total_actual", 0)
    total_budget  = pack.get("total_budget", 0)
    kpi           = pack.get("kpi_summary", [{}])[0]
    mom_pct       = float(kpi.get("MoM %") or 0)

    # Build actuals array
    actuals = [float(p.get("actual", 0)) for p in period_series]
    budgets = [float(p.get("budget", 0)) for p in period_series]
    labels  = [str(p.get("period", "")) for p in period_series]

    if len(actuals) < 2:
        # Not enough data — use simple MoM extrapolation
        actuals = [total_actual * (0.9 + i * 0.02) for i in range(6)]
        budgets = [total_budget * (0.9 + i * 0.02) for i in range(6)]
        labels  = [f"P{i+1}" for i in range(6)]

    # Linear trend on actuals
    x = np.arange(len(actuals))
    if len(x) >= 2:
        coeffs    = np.polyfit(x, actuals, 1)
        slope     = float(coeffs[0])
        intercept = float(coeffs[1])
    else:
        slope, intercept = 0, actuals[-1] if actuals else 0

    # Std dev for confidence bands
    residuals = [a - (slope * i + intercept) for i, a in enumerate(actuals)]
    std_dev   = float(np.std(residuals)) if len(residuals) > 1 else abs(slope) * 0.15

    # Forecast 12 months forward
    n_hist    = len(actuals)
    fc_months = []
    for i in range(12):
        idx      = n_hist + i
        base_fc  = slope * idx + intercept
        growth   = base_fc * (1 + mom_pct * 0.3)  # damped MoM
        fc_months.append({
            "label":       f"F+{i+1}",
            "forecast":    round(max(growth, 0), 0),
            "upper":       round(max(growth + 1.96 * std_dev, 0), 0),
            "lower":       round(max(growth - 1.96 * std_dev, 0), 0),
        })

    # Scenarios
    last_fc = fc_months[-1]["forecast"] if fc_months else total_actual
    monthly_base = fc_months[0]["forecast"] if fc_months else total_actual

    scenarios = {
        "optimistic":  round(monthly_base * 1.07, 0),
        "base":        round(monthly_base * 1.03, 0),
        "pessimistic": round(monthly_base * 0.97, 0),
    }

    # Income/cost split (use account rows if available)
    account_rows = pack.get("account_rows", [])
    income_total = 0.0
    cost_total   = 0.0

    for row in account_rows:
        val = float(row.get("actual", 0))
        # Heuristic: positive = income, negative = cost
        if val >= 0:
            income_total += val
        else:
            cost_total += abs(val)

    if income_total == 0 and cost_total == 0:
        income_total = total_actual * 0.6
        cost_total   = total_actual * 0.4

    income_forecast = income_total * (1 + mom_pct)
    cost_forecast   = cost_total   * (1 + mom_pct * 0.7)
    margin          = ((income_forecast - cost_forecast) / income_forecast * 100) if income_forecast else 0

    return {
        "history":          [{"label": l, "actual": a, "budget": b} for l, a, b in zip(labels, actuals, budgets)],
        "forecast":         fc_months,
        "scenarios":        scenarios,
        "income_forecast":  round(income_forecast, 0),
        "cost_forecast":    round(cost_forecast, 0),
        "margin_pct":       round(margin, 1),
        "growth_rate":      round(mom_pct * 100, 1),
        "trend_slope":      round(slope, 0),
    }


# ═══════════════════════════════════════════════════════════════════
# PPTX BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_pptx(pack: dict, spec: dict, ai_context: str = "") -> bytes:
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt
    import pptx.oxml.ns as pns
    from lxml import etree

    # ── Colours ──
    C_BG     = RGBColor(0x0D, 0x0D, 0x12)
    C_CARD   = RGBColor(0x0F, 0x0F, 0x16)
    C_WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
    C_MUTED  = RGBColor(0xA0, 0xA0, 0xB8)
    C_FAINT  = RGBColor(0x44, 0x44, 0x5A)
    C_ACCENT = RGBColor(0x6C, 0x63, 0xFF)
    C_GREEN  = RGBColor(0x22, 0xC5, 0x5E)
    C_RED    = RGBColor(0xEF, 0x44, 0x44)
    C_AMBER  = RGBColor(0xF5, 0x9E, 0x0B)

    # ── Data ──
    current_period  = pack.get("current_period",  "—")
    previous_period = pack.get("previous_period", "—")
    total_actual    = float(pack.get("total_actual", 0))
    total_budget    = float(pack.get("total_budget", 0))
    variance        = total_actual - total_budget
    var_pct         = safe_pct(total_actual, total_budget) or 0
    kpi             = pack.get("kpi_summary", [{}])[0]
    mom_pct         = float(kpi.get("MoM %") or 0)
    narrative       = pack.get("narrative", "")
    top_budget      = pack.get("top_budget", [])[:8]
    top_mom         = pack.get("top_mom",    [])[:8]
    report_type     = spec.get("report_type",  "monthly")
    tone            = spec.get("tone",         "professional")
    context_text    = spec.get("context",      "") or ai_context
    title_override  = spec.get("title",        "")
    period_series   = pack.get("period_series", [])
    fc_data         = build_forecast_data(pack)

    REPORT_TITLES = {
        "income_statement":  "Resultaträkning",
        "balance_sheet":     "Balansräkning",
        "cash_flow":         "Kassaflödesanalys",
        "budget_vs_actual":  "Budget vs Utfall",
        "ai_summary":        "AI-genererad sammanfattning",
        "monthly":           "Månadsrapport",
        "quarterly":         "Kvartalsrapport",
        "annual":            "Årsredovisning",
    }
    report_title = title_override or REPORT_TITLES.get(report_type, "Finansiell rapport")

    # ── OpenAI narrative ──
    ai_insights = []
    openai_key  = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI
            client   = OpenAI(api_key=openai_key)
            prompt   = (
                f"Du är en senior finansanalytiker. Skapa 4 korta insikter (max 2 meningar var) "
                f"för en {report_title} på svenska. Ton: {tone}. "
                f"Data: Utfall {fmt_sek(total_actual)}, Budget {fmt_sek(total_budget)}, "
                f"Avvikelse {fmt_sek(variance)} ({var_pct*100:+.1f}%), "
                f"MoM {mom_pct*100:+.1f}%. "
                f"Topp-avvikelser: {', '.join([r.get('Label','') + ' ' + fmt_sek(r.get('Vs budget diff',0)) for r in top_budget[:3]])}. "
                + (f"Extra kontext: {context_text}" if context_text else "")
                + " Returnera JSON: [{\"title\": \"...\", \"text\": \"...\"}]"
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=600,
            )
            raw = resp.choices[0].message.content or "[]"
            raw = re.sub(r"```json|```", "", raw).strip()
            ai_insights = json.loads(raw)
        except Exception:
            pass

    if not ai_insights:
        # Fallback rule-based insights
        ai_insights = [
            {"title": "Utfall vs budget",
             "text": f"Utfall {fmt_sek(total_actual)} mot budget {fmt_sek(total_budget)}. Avvikelse {fmt_sek(variance)} ({var_pct*100:+.1f}%)."},
            {"title": "MoM-trend",
             "text": f"Förändring mot föregående period: {mom_pct*100:+.1f}%."},
            {"title": "Största avvikelse",
             "text": f"{top_budget[0].get('Label','—')}: {fmt_sek(top_budget[0].get('Vs budget diff',0))} vs budget." if top_budget else "Inga avvikelser identifierade."},
            {"title": "Narrativ", "text": narrative},
        ]
        if context_text:
            ai_insights.append({"title": "Notering", "text": context_text})

    # ══════════════════════════════════════════════
    # HELPER FUNCTIONS FOR SLIDE BUILDING
    # ══════════════════════════════════════════════

    prs = Presentation()
    prs.slide_width  = Inches(13.33)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]  # completely blank

    def add_rect(slide, x, y, w, h, color: RGBColor, radius: int = 0):
        shape = slide.shapes.add_shape(1, Inches(x), Inches(y), Inches(w), Inches(h))
        shape.fill.solid()
        shape.fill.fore_color.rgb = color
        shape.line.fill.background()
        return shape

    def add_text(slide, text, x, y, w, h, size=18, bold=False,
                 color: RGBColor = C_WHITE, align=PP_ALIGN.LEFT, wrap=True):
        txb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf  = txb.text_frame
        tf.word_wrap = wrap
        p   = tf.paragraphs[0]
        p.alignment = align
        run = p.add_run()
        run.text = str(text)
        run.font.size  = Pt(size)
        run.font.bold  = bold
        run.font.color.rgb = color
        return txb

    def bg(slide):
        add_rect(slide, 0, 0, 13.33, 7.5, C_BG)

    def card(slide, x, y, w, h):
        add_rect(slide, x, y, w, h, C_CARD)

    def kpi_card(slide, x, y, label, value, delta=None, delta_pos=True):
        card(slide, x, y, 2.8, 1.3)
        add_text(slide, label, x+0.15, y+0.1,  2.5, 0.3, size=9,  color=C_FAINT)
        add_text(slide, value, x+0.15, y+0.35, 2.5, 0.5, size=20, bold=True, color=C_WHITE)
        if delta:
            col = C_GREEN if delta_pos else C_RED
            add_text(slide, delta, x+0.15, y+0.9, 2.5, 0.3, size=9, color=col)

    def bar_chart_slide(slide, data, x, y, w, h, title=""):
        """Simple bar chart using rectangles."""
        if title:
            add_text(slide, title, x, y, w, 0.3, size=11, bold=True, color=C_WHITE)
            y += 0.35
            h -= 0.35

        if not data:
            return

        max_val = max([max(abs(d.get("actual", 0)), abs(d.get("budget", 0))) for d in data], default=1)
        if max_val == 0:
            max_val = 1

        bar_w   = w / len(data)
        bar_area = h - 0.4

        for i, d in enumerate(data):
            bx = x + i * bar_w
            # Budget bar (lighter)
            bh = (abs(d.get("budget", 0)) / max_val) * bar_area * 0.9
            if bh > 0.05:
                add_rect(slide, bx + bar_w*0.1, y + bar_area - bh, bar_w*0.35, bh, C_FAINT)
            # Actual bar
            ah = (abs(d.get("actual", 0)) / max_val) * bar_area * 0.9
            if ah > 0.05:
                add_rect(slide, bx + bar_w*0.5, y + bar_area - ah, bar_w*0.35, ah, C_ACCENT)
            # Label
            label = str(d.get("Label") or d.get("Konto") or "")[:8]
            add_text(slide, label, bx, y + bar_area + 0.02, bar_w, 0.25, size=7, color=C_FAINT, align=PP_ALIGN.CENTER)

    def variance_table_slide(slide, rows, x, y, w, title=""):
        if title:
            add_text(slide, title, x, y, w, 0.3, size=11, bold=True, color=C_WHITE)
            y += 0.35

        col_widths = [w*0.35, w*0.15, w*0.15, w*0.15, w*0.2]
        headers    = ["Konto", "Utfall", "Budget", "Avvikelse", "Avv %"]
        # Header row
        hx = x
        for hi, (header, cw) in enumerate(zip(headers, col_widths)):
            add_text(slide, header, hx, y, cw - 0.05, 0.22, size=8, color=C_FAINT, bold=True)
            hx += cw

        y += 0.25
        for row in rows[:8]:
            rx    = x
            avvik = float(row.get("Vs budget diff", 0))
            avpct = float(row.get("Vs budget %",    0))
            col   = C_GREEN if avvik >= 0 else C_RED
            vals  = [
                str(row.get("Label") or row.get("Konto") or "")[:22],
                fmt_sek(float(row.get("Utfall", 0))),
                fmt_sek(float(row.get("Budget", 0))),
                fmt_sek(avvik),
                f"{avpct*100:+.1f}%",
            ]
            for vi, (val, cw) in enumerate(zip(vals, col_widths)):
                vc = col if vi >= 3 else C_MUTED
                add_text(slide, val, rx, y, cw - 0.05, 0.22, size=8, color=vc)
                rx += cw
            y += 0.24
            if y > 7.0:
                break

    # ══════════════════════════════════════════════
    # SLIDE 1 — TITLE
    # ══════════════════════════════════════════════
    s1 = prs.slides.add_slide(blank)
    bg(s1)
    # Accent bar left
    add_rect(s1, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s1, "NORDSHEET", 0.3, 0.4, 6, 0.4, size=11, color=C_FAINT)
    add_text(s1, report_title, 0.3, 1.1, 9, 1.0, size=40, bold=True, color=C_WHITE)
    add_text(s1, f"Period: {current_period}", 0.3, 2.3, 6, 0.4, size=16, color=C_MUTED)
    if previous_period:
        add_text(s1, f"Jämförs med: {previous_period}", 0.3, 2.75, 6, 0.35, size=13, color=C_FAINT)
    if context_text:
        add_text(s1, f"Fokus: {context_text}", 0.3, 3.2, 9, 0.4, size=12, color=C_ACCENT)

    # Big KPI numbers on title slide
    add_text(s1, fmt_sek(total_actual), 0.3, 4.2, 4, 0.7, size=28, bold=True,
             color=C_WHITE if variance >= 0 else C_RED)
    add_text(s1, "Totalt utfall", 0.3, 4.95, 4, 0.3, size=10, color=C_FAINT)
    add_text(s1, f"{fmt_sek(variance)} ({var_pct*100:+.1f}%)",
             0.3, 5.35, 5, 0.35, size=13, color=C_GREEN if variance >= 0 else C_RED)
    add_text(s1, "vs budget", 0.3, 5.7, 3, 0.25, size=9, color=C_FAINT)

    # ══════════════════════════════════════════════
    # SLIDE 2 — KPI OVERVIEW
    # ══════════════════════════════════════════════
    s2 = prs.slides.add_slide(blank)
    bg(s2)
    add_rect(s2, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s2, "Nyckeltal", 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
    add_text(s2, f"Period {current_period}", 0.3, 0.72, 8, 0.3, size=11, color=C_FAINT)

    kpi_cards = [
        ("Totalt utfall",    fmt_sek(total_actual),  None,                              True),
        ("Budget",           fmt_sek(total_budget),  None,                              True),
        ("Avvikelse",        fmt_sek(variance),      f"{var_pct*100:+.1f}% vs budget",  variance >= 0),
        ("MoM-förändring",   f"{mom_pct*100:+.1f}%", f"vs {previous_period or '—'}",    mom_pct >= 0),
    ]
    for i, (lbl, val, delta, pos) in enumerate(kpi_cards):
        kpi_card(s2, 0.3 + i * 3.1, 1.3, lbl, val, delta, pos)

    # Narrative box
    card(s2, 0.3, 2.9, 12.6, 1.6)
    add_text(s2, "AI-sammanfattning", 0.5, 2.95, 12, 0.3, size=10, bold=True, color=C_ACCENT)
    add_text(s2, narrative, 0.5, 3.28, 12.1, 1.1, size=11, color=C_MUTED)

    # ══════════════════════════════════════════════
    # SLIDE 3 — BUDGET VS UTFALL CHART
    # ══════════════════════════════════════════════
    s3 = prs.slides.add_slide(blank)
    bg(s3)
    add_rect(s3, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s3, "Budget vs Utfall", 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
    add_text(s3, "Topp konton efter avvikelse", 0.3, 0.72, 8, 0.3, size=11, color=C_FAINT)

    # Bar chart — top 8 accounts
    bar_data = top_budget[:8]
    bar_chart_slide(s3, bar_data, 0.3, 1.1, 8.2, 4.0)

    # Legend
    add_rect(s3, 0.3, 5.3, 0.25, 0.15, C_ACCENT)
    add_text(s3, "Utfall", 0.6, 5.27, 1.5, 0.2, size=9, color=C_MUTED)
    add_rect(s3, 1.8, 5.3, 0.25, 0.15, C_FAINT)
    add_text(s3, "Budget", 2.1, 5.27, 1.5, 0.2, size=9, color=C_MUTED)

    # Side KPIs
    card(s3, 8.8, 1.1, 4.2, 1.2)
    add_text(s3, "Total avvikelse", 9.0, 1.15, 3.8, 0.25, size=9, color=C_FAINT)
    add_text(s3, fmt_sek(variance), 9.0, 1.45, 3.8, 0.55, size=20, bold=True,
             color=C_GREEN if variance >= 0 else C_RED)
    add_text(s3, f"{var_pct*100:+.1f}% vs budget", 9.0, 2.0, 3.8, 0.25, size=9,
             color=C_GREEN if variance >= 0 else C_RED)

    card(s3, 8.8, 2.5, 4.2, 1.2)
    add_text(s3, "MoM-trend", 9.0, 2.55, 3.8, 0.25, size=9, color=C_FAINT)
    add_text(s3, f"{mom_pct*100:+.1f}%", 9.0, 2.85, 3.8, 0.55, size=20, bold=True,
             color=C_GREEN if mom_pct >= 0 else C_RED)

    # ══════════════════════════════════════════════
    # SLIDE 4 — VARIANCE TABLE
    # ══════════════════════════════════════════════
    s4 = prs.slides.add_slide(blank)
    bg(s4)
    add_rect(s4, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s4, "Avvikelseanalys", 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
    add_text(s4, "Detaljerade avvikelser per konto", 0.3, 0.72, 8, 0.3, size=11, color=C_FAINT)

    # Negative variances (left)
    card(s4, 0.3, 1.1, 6.0, 5.8)
    add_text(s4, "Negativa avvikelser (överskridningar)",
             0.5, 1.15, 5.6, 0.3, size=10, bold=True, color=C_RED)
    variance_table_slide(s4, top_budget, 0.5, 1.55, 5.6)

    # Positive variances (right)
    card(s4, 6.7, 1.1, 6.0, 5.8)
    add_text(s4, "Positiva avvikelser (besparingar)",
             6.9, 1.15, 5.6, 0.3, size=10, bold=True, color=C_GREEN)
    variance_table_slide(s4, top_mom, 6.9, 1.55, 5.6)

    # ══════════════════════════════════════════════
    # SLIDE 5 — TREND / PERIOD SERIES
    # ══════════════════════════════════════════════
    if len(period_series) >= 2:
        s5 = prs.slides.add_slide(blank)
        bg(s5)
        add_rect(s5, 0, 0, 0.08, 7.5, C_ACCENT)
        add_text(s5, "Historisk trend", 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
        add_text(s5, "Utfall per period", 0.3, 0.72, 8, 0.3, size=11, color=C_FAINT)
        bar_chart_slide(s5, period_series[:12], 0.3, 1.1, 12.6, 5.0)

    # ══════════════════════════════════════════════
    # SLIDE 6 — FORECAST
    # ══════════════════════════════════════════════
    s6 = prs.slides.add_slide(blank)
    bg(s6)
    add_rect(s6, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s6, "Prognos", 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
    add_text(s6, "Baserat på historisk trend och MoM-tillväxt",
             0.3, 0.72, 8, 0.3, size=11, color=C_FAINT)

    fc = fc_data
    scenarios = [
        ("Optimistisk",  fc["scenarios"]["optimistic"],  C_GREEN),
        ("Bas",          fc["scenarios"]["base"],         C_ACCENT),
        ("Pessimistisk", fc["scenarios"]["pessimistic"],  C_RED),
    ]
    for i, (lbl, val, col) in enumerate(scenarios):
        card(s6, 0.3 + i * 4.1, 1.3, 3.8, 1.5)
        add_text(s6, lbl, 0.5 + i*4.1, 1.35, 3.4, 0.3, size=10, color=C_FAINT)
        add_text(s6, fmt_sek(val), 0.5 + i*4.1, 1.65, 3.4, 0.6, size=20, bold=True, color=col)
        add_text(s6, "nästa period", 0.5 + i*4.1, 2.3, 3.4, 0.3, size=9, color=C_FAINT)

    # Forecast mini bars
    fc_bars = fc["forecast"][:6]
    if fc_bars:
        max_fc = max(d["forecast"] for d in fc_bars) or 1
        bar_y  = 3.2
        bar_h_area = 2.8
        bw = 12.6 / len(fc_bars)
        for i, d in enumerate(fc_bars):
            bx = 0.3 + i * bw
            ah = (d["forecast"] / max_fc) * bar_h_area * 0.85
            add_rect(s6, bx + bw*0.15, bar_y + bar_h_area - ah, bw * 0.7, ah, C_ACCENT)
            add_text(s6, f"F+{i+1}", bx, bar_y + bar_h_area + 0.05, bw, 0.2,
                     size=8, color=C_FAINT, align=PP_ALIGN.CENTER)
            add_text(s6, fmt_sek(d["forecast"]), bx, bar_y + bar_h_area - ah - 0.25, bw, 0.22,
                     size=8, color=C_MUTED, align=PP_ALIGN.CENTER)

    # ══════════════════════════════════════════════
    # SLIDE 7 — AI INSIGHTS
    # ══════════════════════════════════════════════
    s7 = prs.slides.add_slide(blank)
    bg(s7)
    add_rect(s7, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s7, "Insikter & Rekommendationer",
             0.3, 0.25, 10, 0.45, size=22, bold=True, color=C_WHITE)
    add_text(s7, "AI-genererade observationer baserade på Excel-data",
             0.3, 0.72, 10, 0.3, size=11, color=C_FAINT)

    cols = 2
    for i, insight in enumerate(ai_insights[:6]):
        col_i = i % cols
        row_i = i // cols
        cx = 0.3 + col_i * 6.5
        cy = 1.2 + row_i * 2.1
        card(s7, cx, cy, 6.2, 1.9)
        add_text(s7, insight.get("title", ""), cx + 0.2, cy + 0.12, 5.8, 0.3,
                 size=11, bold=True, color=C_ACCENT)
        add_text(s7, insight.get("text", ""), cx + 0.2, cy + 0.48, 5.8, 1.3,
                 size=10, color=C_MUTED, wrap=True)

    # ══════════════════════════════════════════════
    # REPORT-TYPE SPECIFIC SLIDES
    # ══════════════════════════════════════════════

    if report_type in ("income_statement", "monthly", "quarterly", "annual"):
        # Extra slide: detailed P&L breakdown
        s8 = prs.slides.add_slide(blank)
        bg(s8)
        add_rect(s8, 0, 0, 0.08, 7.5, C_ACCENT)
        add_text(s8, "Resultaträkning — detalj",
                 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
        variance_table_slide(s8, top_budget + top_mom, 0.3, 1.0, 12.6)

    if report_type == "budget_vs_actual":
        # Extra slide: waterfall-style comparison
        s9 = prs.slides.add_slide(blank)
        bg(s9)
        add_rect(s9, 0, 0, 0.08, 7.5, C_ACCENT)
        add_text(s9, "Budget vs Utfall — alla konton",
                 0.3, 0.25, 8, 0.45, size=22, bold=True, color=C_WHITE)
        variance_table_slide(s9, top_budget + top_mom, 0.3, 1.0, 12.6)

    # ══════════════════════════════════════════════
    # SLIDE LAST — THANK YOU
    # ══════════════════════════════════════════════
    s_end = prs.slides.add_slide(blank)
    bg(s_end)
    add_rect(s_end, 0, 0, 0.08, 7.5, C_ACCENT)
    add_text(s_end, "NORDSHEET", 0.3, 2.8, 12, 0.4, size=11, color=C_FAINT, align=PP_ALIGN.CENTER)
    add_text(s_end, "Finance intelligence for the modern controller.",
             0.3, 3.3, 12, 0.6, size=18, color=C_MUTED, align=PP_ALIGN.CENTER)
    add_text(s_end, f"Rapport genererad för period {current_period}",
             0.3, 4.1, 12, 0.35, size=11, color=C_FAINT, align=PP_ALIGN.CENTER)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════════════════════════
# DOCX BUILDER
# ═══════════════════════════════════════════════════════════════════

def build_docx(pack: dict, spec: dict, ai_context: str = "") -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor, Inches, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    doc = Document()

    # Page margins
    for section in doc.sections:
        section.top_margin    = Cm(2.0)
        section.bottom_margin = Cm(2.0)
        section.left_margin   = Cm(2.5)
        section.right_margin  = Cm(2.5)

    # Data
    current_period  = pack.get("current_period",  "—")
    previous_period = pack.get("previous_period", "—")
    total_actual    = float(pack.get("total_actual", 0))
    total_budget    = float(pack.get("total_budget", 0))
    variance        = total_actual - total_budget
    var_pct         = safe_pct(total_actual, total_budget) or 0
    kpi             = pack.get("kpi_summary", [{}])[0]
    mom_pct         = float(kpi.get("MoM %") or 0)
    narrative       = pack.get("narrative", "")
    top_budget      = pack.get("top_budget", [])
    top_mom         = pack.get("top_mom",    [])
    report_type     = spec.get("report_type",  "monthly")
    tone            = spec.get("tone",         "professional")
    context_text    = spec.get("context",      "") or ai_context

    REPORT_TITLES = {
        "income_statement": "Resultaträkning",
        "balance_sheet":    "Balansräkning",
        "cash_flow":        "Kassaflödesanalys",
        "budget_vs_actual": "Budget vs Utfall",
        "ai_summary":       "AI-genererad sammanfattning",
        "monthly":          "Månadsrapport",
        "quarterly":        "Kvartalsrapport",
        "annual":           "Årsredovisning",
    }
    report_title = spec.get("title") or REPORT_TITLES.get(report_type, "Finansiell rapport")

    # AI narrative
    openai_key = os.getenv("OPENAI_API_KEY")
    full_narrative = narrative
    executive_summary = ""

    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            prompt = (
                f"Du är en senior finansanalytiker. Skriv en {report_title} på svenska med ton: {tone}. "
                f"Inkludera: executive summary (2 stycken), analys av avvikelser, "
                f"rekommendationer och slutsats. "
                f"Data: Utfall {fmt_sek(total_actual)}, Budget {fmt_sek(total_budget)}, "
                f"Avvikelse {fmt_sek(variance)} ({var_pct*100:+.1f}%), MoM {mom_pct*100:+.1f}%. "
                f"Topp-avvikelser: {', '.join([r.get('Label','') + ' ' + fmt_sek(r.get('Vs budget diff',0)) for r in top_budget[:5]])}. "
                + (f"Extra instruktion: {context_text}" if context_text else "")
                + " Returnera JSON: {\"executive_summary\": \"...\", \"analysis\": \"...\", \"recommendations\": \"...\", \"conclusion\": \"...\"}"
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
            )
            raw = re.sub(r"```json|```", "", resp.choices[0].message.content or "{}").strip()
            ai_text = json.loads(raw)
            executive_summary = ai_text.get("executive_summary", "")
            full_narrative     = ai_text.get("analysis", narrative)
        except Exception:
            executive_summary = narrative

    def h1(text):
        p = doc.add_heading(text, level=1)
        p.style.font.color.rgb = RGBColor(0x6C, 0x63, 0xFF)

    def h2(text):
        doc.add_heading(text, level=2)

    def para(text, italic=False):
        p = doc.add_paragraph(text)
        if italic:
            for run in p.runs:
                run.italic = True
        return p

    def kv(label, value, color=None):
        p = doc.add_paragraph()
        r1 = p.add_run(f"{label}: ")
        r1.bold = True
        r2 = p.add_run(value)
        if color:
            r2.font.color.rgb = color

    # ── Cover ──
    doc.add_heading("NORDSHEET", 0)
    doc.add_heading(report_title, 0)
    para(f"Period: {current_period}")
    if previous_period:
        para(f"Jämförs med: {previous_period}")
    if context_text:
        para(f"Fokus: {context_text}", italic=True)
    doc.add_page_break()

    # ── Executive Summary ──
    h1("Executive Summary")
    para(executive_summary or narrative)
    doc.add_paragraph()

    # ── KPI ──
    h1("Nyckeltal")
    kv("Totalt utfall",   fmt_sek(total_actual))
    kv("Budget",          fmt_sek(total_budget))
    kv("Avvikelse",       f"{fmt_sek(variance)} ({var_pct*100:+.1f}%)",
       RGBColor(0x22,0xC5,0x5E) if variance >= 0 else RGBColor(0xEF,0x44,0x44))
    kv("MoM-förändring",  f"{mom_pct*100:+.1f}% vs {previous_period or '—'}",
       RGBColor(0x22,0xC5,0x5E) if mom_pct >= 0 else RGBColor(0xEF,0x44,0x44))

    doc.add_paragraph()

    # ── Analysis ──
    h1("Analys")
    para(full_narrative)

    # ── Variance Table ──
    h1("Avvikelseanalys")
    h2("Negativa avvikelser (överskridningar)")

    if top_budget:
        t = doc.add_table(rows=1, cols=5)
        t.style = "Table Grid"
        hdr = t.rows[0].cells
        for i, col in enumerate(["Konto", "Utfall", "Budget", "Avvikelse", "Avv %"]):
            hdr[i].text = col
            hdr[i].paragraphs[0].runs[0].bold = True

        for row in top_budget[:10]:
            cells = t.add_row().cells
            cells[0].text = str(row.get("Label") or row.get("Konto") or "")
            cells[1].text = fmt_sek(float(row.get("Utfall", 0)))
            cells[2].text = fmt_sek(float(row.get("Budget", 0)))
            cells[3].text = fmt_sek(float(row.get("Vs budget diff", 0)))
            cells[4].text = f"{float(row.get('Vs budget %', 0))*100:+.1f}%"

    doc.add_paragraph()
    h2("Positiva avvikelser (besparingar)")

    if top_mom:
        t2 = doc.add_table(rows=1, cols=5)
        t2.style = "Table Grid"
        hdr2 = t2.rows[0].cells
        for i, col in enumerate(["Konto", "Utfall", "Budget", "Avvikelse", "Avv %"]):
            hdr2[i].text = col
            hdr2[i].paragraphs[0].runs[0].bold = True
        for row in top_mom[:10]:
            cells = t2.add_row().cells
            cells[0].text = str(row.get("Label") or row.get("Konto") or "")
            cells[1].text = fmt_sek(float(row.get("Utfall", 0)))
            cells[2].text = fmt_sek(float(row.get("Budget", 0)))
            cells[3].text = fmt_sek(float(row.get("Vs budget diff", 0)))
            cells[4].text = f"{float(row.get('Vs budget %', 0))*100:+.1f}%"

    # ── Forecast ──
    h1("Prognos")
    fc = build_forecast_data(pack)
    kv("Inkomstprognos",   fmt_sek(fc["income_forecast"]))
    kv("Kostnadsprognos",  fmt_sek(fc["cost_forecast"]))
    kv("Lönsamhetsprognos", f"{fc['margin_pct']:.1f}%")
    doc.add_paragraph()
    kv("Optimistiskt scenario",  fmt_sek(fc["scenarios"]["optimistic"]))
    kv("Basscenario",            fmt_sek(fc["scenarios"]["base"]))
    kv("Pessimistiskt scenario", fmt_sek(fc["scenarios"]["pessimistic"]))

    # ── Recommendations ──
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            rp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content":
                    f"Ge 3-5 konkreta rekommendationer på svenska för en {report_title}. "
                    f"Data: avvikelse {fmt_sek(variance)}, MoM {mom_pct*100:+.1f}%. "
                    f"Returnera JSON: [\"...\", \"...\"]"}],
                max_tokens=400,
            )
            raw_r = re.sub(r"```json|```", "", rp.choices[0].message.content or "[]").strip()
            recs  = json.loads(raw_r)
            h1("Rekommendationer")
            for rec in recs:
                doc.add_paragraph(f"• {rec}")
        except Exception:
            pass

    # ── Conclusion ──
    h1("Slutsats")
    para(
        f"Sammanfattningsvis visar {current_period} ett utfall på {fmt_sek(total_actual)} "
        f"mot budget {fmt_sek(total_budget)}, en avvikelse på {fmt_sek(variance)} ({var_pct*100:+.1f}%). "
        f"MoM-förändringen är {mom_pct*100:+.1f}%."
    )

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df = read_upload_bytes(contents, file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Kunde inte läsa filen: {e}")
    cols = list(df.columns)
    return {
        "available_columns": cols,
        "column_suggestions": suggest_columns(cols),
        "row_count": len(df),
        "preview": df.head(20).fillna("").to_dict(orient="records"),
    }


@app.post("/api/ai-map")
async def ai_map(file: UploadFile = File(...)):
    """
    Reads the file and uses OpenAI to intelligently map columns.
    Returns both the AI mapping and the keyword-based fallback.
    """
    try:
        contents = await file.read()
        df = read_upload_bytes(contents, file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Kunde inte läsa filen: {e}")

    cols = list(df.columns)
    preview = df.head(5).fillna("").to_dict(orient="records")
    keyword_suggestions = suggest_columns(cols)

    openai_key = os.getenv("OPENAI_API_KEY")
    ai_mapping = None

    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)

            prompt = f"""Du är en expert på finansiell data. Analysera dessa kolumnnamn och förhandsvisning från en Excel/CSV-fil och mappa dem till rätt finansiella fält.

Kolumner: {json.dumps(cols, ensure_ascii=False)}

Förhandsvisning (5 rader):
{json.dumps(preview, ensure_ascii=False)}

Mappa till dessa fält (returnera null om ingen lämplig kolumn finns):
- period: kolumn med datum/period/månad
- account: kolumn med kontonummer
- account_name: kolumn med kontonamn/beskrivning
- actual: kolumn med faktiskt utfall/belopp
- budget: kolumn med budgeterat belopp
- entity: kolumn med bolag/enhet
- cost_center: kolumn med kostnadsställe
- project: kolumn med projekt

Returnera ENDAST giltig JSON utan förklaringar:
{{"period": "kolumnnamn eller null", "account": "kolumnnamn eller null", "account_name": "kolumnnamn eller null", "actual": "kolumnnamn eller null", "budget": "kolumnnamn eller null", "entity": "kolumnnamn eller null", "cost_center": "kolumnnamn eller null", "project": "kolumnnamn eller null"}}"""

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
            raw = re.sub(r"```json|```", "", raw).strip()
            ai_mapping = json.loads(raw)

            # Validate that mapped columns actually exist
            for field, col in ai_mapping.items():
                if col and col not in cols:
                    ai_mapping[field] = None

        except Exception as e:
            ai_mapping = None

    # Build final mapping: AI first, keyword fallback
    fields = ["period", "account", "account_name", "actual", "budget", "entity", "cost_center", "project"]
    final_mapping = {}
    mapping_source = {}

    for field in fields:
        ai_val = (ai_mapping or {}).get(field)
        kw_val = keyword_suggestions.get(field, [None])[0] if keyword_suggestions.get(field) else None

        if ai_val:
            final_mapping[field] = ai_val
            mapping_source[field] = "ai"
        elif kw_val:
            final_mapping[field] = kw_val
            mapping_source[field] = "keyword"
        else:
            final_mapping[field] = ""
            mapping_source[field] = "none"

    return {
        "available_columns": cols,
        "column_suggestions": keyword_suggestions,
        "mapping": final_mapping,
        "mapping_source": mapping_source,
        "ai_used": ai_mapping is not None,
        "row_count": len(df),
        "preview": preview,
    }


@app.post("/api/analyze-with-mapping")
async def analyze_with_mapping(
    file: UploadFile = File(...),
    mapping_json: str = Form(...),
):
    try:
        contents = await file.read()
        df = read_upload_bytes(contents, file.filename or "")
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Kunde inte läsa filen: {e}")
    try:
        mapping = json.loads(mapping_json)
    except Exception:
        raise HTTPException(status_code=422, detail="Ogiltig mapping JSON")
    try:
        pack = compute_pack(df, mapping)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysen misslyckades: {e}")
    return pack


class ChatRequest(BaseModel):
    question: str
    pack: Any


@app.post("/api/chat")
async def chat(req: ChatRequest):
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)
            system = (
                "Du är en senior finansanalytiker på NordSheet. Svara kort och konkret på svenska. "
                f"Analysdata: {json.dumps(req.pack, ensure_ascii=False)}"
            )
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": req.question},
                ],
                max_tokens=500,
            )
            return {"answer": resp.choices[0].message.content}
        except Exception as e:
            return {"answer": f"AI-fel: {e}"}

    pack = req.pack or {}
    q    = req.question.lower()
    if any(w in q for w in ["utfall","actual","total"]):
        return {"answer": f"Totalt utfall: {fmt_sek(float(pack.get('total_actual',0)))}"}
    if "budget" in q:
        return {"answer": f"Total budget: {fmt_sek(float(pack.get('total_budget',0)))}"}
    if "period" in q:
        return {"answer": f"Aktuell period: {pack.get('current_period','N/A')}"}
    return {"answer": pack.get("narrative", "Ingen data tillgänglig.")}


class ExportRequest(BaseModel):
    fmt:           str
    pack:          Any
    report_items:  list = []
    spec:          dict = {}
    purpose:       str  = ""
    business_type: str  = ""


@app.post("/api/export")
async def export_file(req: ExportRequest):
    pack        = req.pack or {}
    spec        = req.spec or {}
    ai_context  = spec.get("context", "")

    if req.fmt == "pptx":
        try:
            data = build_pptx(pack, spec, ai_context)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"PPTX-generering misslyckades: {e}")
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            headers={"Content-Disposition": 'attachment; filename="nordsheet_rapport.pptx"'},
        )

    elif req.fmt == "docx":
        try:
            data = build_docx(pack, spec, ai_context)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"DOCX-generering misslyckades: {e}")
        return StreamingResponse(
            io.BytesIO(data),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": 'attachment; filename="nordsheet_rapport.docx"'},
        )

    raise HTTPException(status_code=400, detail=f"Okänt format: {req.fmt}")


@app.post("/api/forecast")
async def forecast_api(req: ChatRequest):
    """Return forecast data computed from pack."""
    try:
        fc = build_forecast_data(req.pack or {})
        return fc
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/forecast")
async def forecast_get():
    return {"message": "POST to /api/forecast with pack data."}
