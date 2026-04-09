"""
NordSheet — Intelligent Alerts Engine
======================================
Lägg till i main.py:
    from alerts_engine import alerts_router
    app.include_router(alerts_router)

Denna modul gör det en senior controller gör mentalt:
- Tittar på ALLA konton och perioder
- Bestämmer vad som FAKTISKT spelar roll
- Returnerar bara det som kräver uppmärksamhet
- Inkluderar underliggande transaktioner för spårbarhet
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any, Optional
import pandas as pd
import numpy as np
import json, os, re

alerts_router = APIRouter(prefix="/api", tags=["alerts"])


def safe_pct(a: float, b: float) -> Optional[float]:
    if b and b != 0:
        return (a - b) / abs(b)
    return None


def fmt_sek(n: float) -> str:
    abs_n = abs(n)
    if abs_n >= 1_000_000:
        return f"{n/1_000_000:.1f} MSEK"
    if abs_n >= 1_000:
        return f"{n/1_000:.0f} tkr"
    return f"{n:.0f} kr"


# ═══════════════════════════════════════════════════════════════════
# DATA ANALYSIS ENGINE (runs BEFORE AI)
# ═══════════════════════════════════════════════════════════════════

def analyze_all_accounts(pack: dict) -> list[dict]:
    """
    Analyserar ALLA konton med tidsserier, persistence, acceleration.
    Returnerar en lista med pre-beräknad data per konto.
    """
    account_rows = pack.get("account_rows", [])
    period_series = pack.get("period_series", [])
    total_actual = abs(float(pack.get("total_actual", 1))) or 1
    total_budget = abs(float(pack.get("total_budget", 0))) or 1
    current_period = pack.get("current_period", "")
    periods = pack.get("periods", [])

    if not account_rows:
        return []

    # Skapa DataFrame från alla rader
    df = pd.DataFrame(account_rows)

    # Säkerställ kolumner
    for col in ["account", "account_name", "actual", "budget", "variance", "variance_pct", "period"]:
        if col not in df.columns:
            df[col] = 0 if col in ["actual", "budget", "variance", "variance_pct"] else ""

    df["actual"] = pd.to_numeric(df["actual"], errors="coerce").fillna(0)
    df["budget"] = pd.to_numeric(df["budget"], errors="coerce").fillna(0)
    df["variance"] = df["actual"] - df["budget"]

    # Om det finns period-kolumn, bygg tidsserier per konto
    has_periods = "period" in df.columns and df["period"].nunique() > 1

    # Aggregera per konto
    if has_periods:
        agg = df.groupby(["account", "account_name"]).agg(
            total_actual=("actual", "sum"),
            total_budget=("budget", "sum"),
        ).reset_index()
        agg["total_variance"] = agg["total_actual"] - agg["total_budget"]
        agg["variance_pct"] = agg.apply(
            lambda r: safe_pct(r["total_actual"], r["total_budget"]), axis=1
        )
    else:
        agg = df.copy()
        agg = agg.rename(columns={
            "actual": "total_actual",
            "budget": "total_budget",
            "variance": "total_variance",
        })

    # Beräkna materialitetsgräns dynamiskt
    # Regel: avvikelser under 0.5% av total omsättning är brus
    materiality_threshold = total_actual * 0.005

    # Remaining months in year
    month_match = re.search(r"(\d{2})$", str(current_period))
    current_month = int(month_match.group(1)) if month_match else 6
    remaining_months = max(12 - current_month, 1)

    results = []
    for _, row in agg.iterrows():
        account_nr = str(row.get("account", ""))
        account_name = str(row.get("account_name", ""))
        total_act = float(row.get("total_actual", 0))
        total_bud = float(row.get("total_budget", 0))
        variance = float(row.get("total_variance", 0))
        var_pct = row.get("variance_pct")
        if var_pct is None:
            var_pct = safe_pct(total_act, total_bud)

        # ── Tidsserier per konto ──
        period_values = []
        if has_periods:
            acct_rows = df[df["account"].astype(str) == account_nr].sort_values("period")
            for _, pr in acct_rows.iterrows():
                period_values.append({
                    "period": str(pr.get("period", "")),
                    "actual": float(pr.get("actual", 0)),
                    "budget": float(pr.get("budget", 0)),
                    "variance": float(pr.get("actual", 0)) - float(pr.get("budget", 0)),
                })

        # ── Persistence: hur många perioder i rad har det avvikit? ──
        streak = 0
        if period_values:
            last_sign = 1 if period_values[-1]["variance"] >= 0 else -1
            for pv in reversed(period_values):
                sign = 1 if pv["variance"] >= 0 else -1
                if sign == last_sign:
                    streak += 1
                else:
                    break

        # ── Acceleration ──
        acceleration = 0.0
        if len(period_values) >= 3:
            recent = [abs(pv["variance"]) for pv in period_values[-3:]]
            diffs = [recent[1] - recent[0], recent[2] - recent[1]]
            acceleration = sum(diffs) / 2

        # ── Helårsprognos ──
        monthly_variance = variance / max(len(periods), 1) if periods else variance
        year_end_impact = monthly_variance * remaining_months

        # ── Kontotyp ──
        nr = int(account_nr) if account_nr.isdigit() else 0
        if 7000 <= nr < 8000:
            account_type = "Personal"
            weight = 3.0
        elif 3000 <= nr < 4000:
            account_type = "Intäkter"
            weight = 2.5
        elif 4000 <= nr < 5000:
            account_type = "Varuinköp"
            weight = 2.0
        elif 5000 <= nr < 6000:
            account_type = "Lokalkostnader"
            weight = 1.5
        elif 8000 <= nr < 9000:
            account_type = "Finansiella"
            weight = 1.2
        elif 6000 <= nr < 7000:
            account_type = "Övriga kostnader"
            weight = 0.8
        else:
            account_type = "Övrigt"
            weight = 1.0

        results.append({
            "account": account_nr,
            "account_name": account_name or account_nr,
            "account_type": account_type,
            "total_actual": round(total_act, 0),
            "total_budget": round(total_bud, 0),
            "variance": round(variance, 0),
            "variance_pct": round(float(var_pct or 0), 4),
            "abs_variance": abs(variance),
            "materiality_ratio": abs(variance) / total_actual,
            "streak_months": streak,
            "acceleration": round(acceleration, 0),
            "year_end_impact": round(year_end_impact, 0),
            "account_weight": weight,
            "period_values": period_values,
            "passes_materiality": abs(variance) >= materiality_threshold,
        })

    return results


# ═══════════════════════════════════════════════════════════════════
# AI TRIAGE — Let AI decide what matters
# ═══════════════════════════════════════════════════════════════════

class AlertsRequest(BaseModel):
    pack: dict


@alerts_router.post("/intelligent-alerts")
async def intelligent_alerts(req: AlertsRequest):
    """
    Huvudendpoint — analyserar all data och låter AI avgöra
    vilka avvikelser en controller behöver se.

    Steg:
    1. Beräkna materialitet, persistence, acceleration för ALLA konton
    2. Filtrera bort allt under materialitetsgränsen
    3. Skicka de kvarvarande till AI för bedömning
    4. AI returnerar bara de som är värda att flagga
    5. Inkludera underliggande transaktioner för varje flaggad avvikelse
    """
    pack = req.pack or {}

    # Steg 1: Analysera alla konton
    all_accounts = analyze_all_accounts(pack)

    if not all_accounts:
        return {"alerts": [], "summary": "Ingen data att analysera."}

    total_actual = abs(float(pack.get("total_actual", 0)))
    total_budget = abs(float(pack.get("total_budget", 0)))
    current_period = pack.get("current_period", "")

    # Steg 2: Pre-filter — ta bort uppenbart brus
    # Behåll konton som passerar materialitetsgränsen ELLER
    # har hög persistence (3+ mån) ELLER hög kontotyp-vikt
    candidates = [
        a for a in all_accounts
        if a["passes_materiality"]
        or a["streak_months"] >= 3
        or (a["account_weight"] >= 2.5 and abs(a["variance_pct"]) > 0.03)
    ]

    if not candidates:
        return {
            "alerts": [],
            "summary": "Inga väsentliga avvikelser hittades.",
            "total_accounts_analyzed": len(all_accounts),
            "materiality_threshold": round(total_actual * 0.005, 0),
        }

    # Steg 3: Bygg AI-prompt
    openai_key = os.getenv("OPENAI_API_KEY")

    if openai_key:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)

            # Bygg kontosammanfattning för AI
            account_summaries = []
            for a in candidates:
                trend_desc = ""
                if a["streak_months"] >= 3 and a["acceleration"] > 0:
                    trend_desc = f"ESKALERANDE - {a['streak_months']} mån i rad, ökar"
                elif a["streak_months"] >= 3:
                    trend_desc = f"Ihållande - {a['streak_months']} mån i rad"
                elif a["streak_months"] >= 2:
                    trend_desc = f"Kort trend - {a['streak_months']} mån"
                else:
                    trend_desc = "Enstaka period"

                account_summaries.append(
                    f"- {a['account']} {a['account_name']} | "
                    f"Typ: {a['account_type']} | "
                    f"Avvikelse: {fmt_sek(a['variance'])} ({a['variance_pct']*100:+.1f}%) | "
                    f"Trend: {trend_desc} | "
                    f"Helårspåverkan: {fmt_sek(a['year_end_impact'])} | "
                    f"Andel av total: {a['materiality_ratio']*100:.1f}%"
                )

            prompt = f"""Du är en senior controller som granskar månadsrapporten.

BOLAGSDATA:
- Total omsättning: {fmt_sek(total_actual)}
- Total budget: {fmt_sek(total_budget)}
- Period: {current_period}
- Antal konton analyserade: {len(all_accounts)}
- Materialitetsgräns: {fmt_sek(total_actual * 0.005)}

KANDIDATER (har passerat pre-filter — {len(candidates)} av {len(all_accounts)} konton):
{chr(10).join(account_summaries)}

UPPGIFT:
Välj ut BARA de avvikelser som en erfaren controller faktiskt skulle reagera på.
Tänk så här:
- Är beloppet väsentligt för detta bolag?
- Har det pågått tillräckligt länge för att vara ett mönster (inte bara timing)?
- Är det ett konto där avvikelser spelar roll (personal, intäkter > kontorsmaterial)?
- Om trenden fortsätter, blir det ett problem vid årsslut?
- Skulle du ta upp detta på ett ledningsmöte?

Du ska INTE flagga:
- Små belopp relativt bolagets storlek
- Enstaka månaders avvikelse som troligen är timing
- Konton med låg vikt (kontorsmaterial, fika, porto)
- Positiva avvikelser som inte kräver åtgärd

Returnera BARA JSON — ingen annan text. Format:
{{
  "flagged": [
    {{
      "account": "7210",
      "severity": "critical|warning|info",
      "headline": "Kort rubrik (max 8 ord)",
      "reasoning": "1-2 meningar: varför detta spelar roll",
      "action": "Konkret nästa steg för controllern",
      "year_end_risk": "Vad händer om inget görs"
    }}
  ],
  "dismissed_reason": "Kort förklaring: varför de andra inte flaggades",
  "overall_assessment": "1-2 meningar om bolagets ekonomiska läge"
}}

Flagga typiskt 2-6 avvikelser. Kan vara 0 om inget är väsentligt. Kan vara fler om läget är allvarligt."""

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du är expert-controller. Returnera BARA giltig JSON."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=1200,
                temperature=0.1,
            )

            raw = re.sub(r"```json|```", "", resp.choices[0].message.content or "{}").strip()
            ai_result = json.loads(raw)

        except Exception as e:
            print(f"[Alerts] AI error: {e}")
            ai_result = None
    else:
        ai_result = None

    # Steg 4: Bygg response
    if ai_result and "flagged" in ai_result:
        flagged_accounts = {f["account"] for f in ai_result["flagged"]}

        alerts = []
        for f in ai_result["flagged"]:
            # Hitta match i candidates
            match = next((a for a in candidates if a["account"] == f["account"]), None)
            if not match:
                continue

            alerts.append({
                # Identifiering
                "account": match["account"],
                "account_name": match["account_name"],
                "account_type": match["account_type"],

                # AI-bedömning
                "severity": f.get("severity", "info"),
                "headline": f.get("headline", ""),
                "reasoning": f.get("reasoning", ""),
                "action": f.get("action", ""),
                "year_end_risk": f.get("year_end_risk", ""),

                # Siffror
                "actual": match["total_actual"],
                "budget": match["total_budget"],
                "variance": match["variance"],
                "variance_pct": match["variance_pct"],
                "streak_months": match["streak_months"],
                "acceleration": match["acceleration"],
                "year_end_impact": match["year_end_impact"],

                # Sparkline-data (sista 6 perioderna)
                "sparkline": [pv["variance"] for pv in match["period_values"][-6:]],

                # Drilldown — alla underliggande transaktioner
                "drilldown": match["period_values"],
            })

        return {
            "alerts": alerts,
            "summary": ai_result.get("overall_assessment", ""),
            "dismissed_reason": ai_result.get("dismissed_reason", ""),
            "total_accounts_analyzed": len(all_accounts),
            "candidates_evaluated": len(candidates),
            "materiality_threshold": round(total_actual * 0.005, 0),
        }

    # Steg 5: Fallback utan AI — använd heuristik
    # Sortera efter composite score
    for a in candidates:
        mat_score = min(a["materiality_ratio"] * 500, 30)
        pct_score = min(abs(a["variance_pct"]) * 40, 20)
        pers_score = min(a["streak_months"] * 5, 20)
        accel_score = min(max(a["acceleration"], 0) / 1000, 15) if a["acceleration"] > 0 else 0
        weight_score = a["account_weight"] * 5
        a["score"] = mat_score + pct_score + pers_score + accel_score + weight_score

    candidates.sort(key=lambda a: a["score"], reverse=True)

    # Ta topp-6 som passerar score > 25
    top = [a for a in candidates if a["score"] > 25][:6]

    alerts = []
    for a in top:
        severity = "critical" if a["score"] >= 55 else "warning" if a["score"] >= 35 else "info"
        alerts.append({
            "account": a["account"],
            "account_name": a["account_name"],
            "account_type": a["account_type"],
            "severity": severity,
            "headline": f"{a['account_name']} avviker {a['variance_pct']*100:+.0f}%",
            "reasoning": f"Avvikelse på {fmt_sek(a['variance'])} ({a['streak_months']} mån trend). Kontotyp: {a['account_type']}.",
            "action": "Undersök underliggande transaktioner.",
            "year_end_risk": f"Helårspåverkan: {fmt_sek(a['year_end_impact'])} om trenden fortsätter.",
            "actual": a["total_actual"],
            "budget": a["total_budget"],
            "variance": a["variance"],
            "variance_pct": a["variance_pct"],
            "streak_months": a["streak_months"],
            "acceleration": a["acceleration"],
            "year_end_impact": a["year_end_impact"],
            "sparkline": [pv["variance"] for pv in a["period_values"][-6:]],
            "drilldown": a["period_values"],
        })

    return {
        "alerts": alerts,
        "summary": f"Heuristisk analys — {len(alerts)} avvikelser av {len(all_accounts)} konton flaggade.",
        "dismissed_reason": "AI ej tillgänglig — använder regelbaserad filtrering.",
        "total_accounts_analyzed": len(all_accounts),
        "candidates_evaluated": len(candidates),
        "materiality_threshold": round(total_actual * 0.005, 0),
    }


# ═══════════════════════════════════════════════════════════════════
# DRILLDOWN — Hämta alla transaktioner för ett specifikt konto
# ═══════════════════════════════════════════════════════════════════

class DrilldownRequest(BaseModel):
    pack: dict
    account: str
    period: Optional[str] = None


@alerts_router.post("/drilldown")
async def drilldown(req: DrilldownRequest):
    """
    Returnerar alla underliggande transaktionsrader för ett konto.
    Om period anges, filtreras på den perioden.
    """
    pack = req.pack or {}
    account_rows = pack.get("account_rows", [])
    raw_rows = pack.get("raw_rows", [])  # om ni sparar originaldatan

    # Sök i account_rows
    matches = []
    for row in account_rows:
        if str(row.get("account", "")) == req.account:
            if req.period and str(row.get("period", "")) != req.period:
                continue
            matches.append({
                "period": str(row.get("period", "")),
                "account": str(row.get("account", "")),
                "account_name": str(row.get("account_name", "")),
                "actual": float(row.get("actual", 0)),
                "budget": float(row.get("budget", 0)),
                "variance": float(row.get("actual", 0)) - float(row.get("budget", 0)),
                "cost_center": str(row.get("cost_center", "")),
                "project": str(row.get("project", "")),
            })

    # Sök i raw_rows (originaldata om tillgänglig)
    raw_matches = []
    for row in raw_rows:
        if str(row.get("account", "")) == req.account:
            if req.period and str(row.get("period", "")) != req.period:
                continue
            raw_matches.append(row)

    return {
        "account": req.account,
        "period_filter": req.period,
        "aggregated": matches,
        "transactions": raw_matches,
        "total_rows": len(matches),
    }


# ═══════════════════════════════════════════════════════════════════
# DATA QUALITY ENGINE — "Vad saknas eller ser konstigt ut?"
# ═══════════════════════════════════════════════════════════════════

def detect_data_issues(pack: dict) -> list[dict]:
    """
    Analyserar hela datasetet och hittar:
    - Saknade perioder (konto bokfört varje månad utom en)
    - Tomma konton som borde ha värden
    - Ovanligt stora/små belopp (outliers)
    - Konton som plötsligt slutat användas
    - Perioder helt utan bokföring
    """
    account_rows = pack.get("account_rows", [])
    periods = pack.get("periods", [])
    current_period = pack.get("current_period", "")
    total_actual = abs(float(pack.get("total_actual", 1))) or 1

    if not account_rows or not periods:
        return []

    df = pd.DataFrame(account_rows)
    for col in ["account", "account_name", "actual", "budget", "period"]:
        if col not in df.columns:
            df[col] = 0 if col in ["actual", "budget"] else ""
    df["actual"] = pd.to_numeric(df["actual"], errors="coerce").fillna(0)
    df["budget"] = pd.to_numeric(df["budget"], errors="coerce").fillna(0)

    has_periods = "period" in df.columns and df["period"].nunique() > 1
    if not has_periods:
        return []

    all_periods = sorted(df["period"].dropna().astype(str).unique().tolist())
    issues = []

    # ── Per konto: bygg profil ──
    for account_nr, grp in df.groupby("account"):
        account_nr = str(account_nr)
        account_name = str(grp["account_name"].iloc[0]) if "account_name" in grp.columns else account_nr
        periods_present = set(grp["period"].astype(str).unique())
        values = grp.sort_values("period")["actual"].tolist()

        # 1. SAKNADE PERIODER
        # Konto som förekommer i de flesta perioder men saknar en/några
        if len(all_periods) >= 4 and len(periods_present) >= len(all_periods) * 0.6:
            missing = [p for p in all_periods if p not in periods_present]
            if 0 < len(missing) <= 2:
                avg_val = grp["actual"].mean()
                if abs(avg_val) > total_actual * 0.002:  # bara om kontot är väsentligt
                    issues.append({
                        "type": "missing_period",
                        "account": account_nr,
                        "account_name": account_name,
                        "detail": {
                            "missing_periods": missing,
                            "expected_periods": len(all_periods),
                            "present_periods": len(periods_present),
                            "avg_value": round(avg_val, 0),
                        },
                    })

        # 2. OUTLIERS — ovanligt stora/små belopp
        if len(values) >= 4:
            arr = np.array(values)
            nonzero = arr[arr != 0]
            if len(nonzero) >= 3:
                median = float(np.median(nonzero))
                std = float(np.std(nonzero))
                if std > 0 and abs(median) > total_actual * 0.001:
                    for _, row in grp.iterrows():
                        val = float(row["actual"])
                        period = str(row.get("period", ""))
                        if val != 0 and abs(val - median) > 2.5 * std:
                            issues.append({
                                "type": "outlier",
                                "account": account_nr,
                                "account_name": account_name,
                                "detail": {
                                    "period": period,
                                    "value": round(val, 0),
                                    "median": round(median, 0),
                                    "deviation_factor": round(abs(val - median) / std, 1),
                                },
                            })

        # 3. KONTO SOM SLUTAT ANVÄNDAS
        # Hade värden i början men noll i senaste perioderna
        if len(values) >= 4:
            first_half = values[:len(values)//2]
            second_half = values[len(values)//2:]
            first_active = sum(1 for v in first_half if v != 0)
            second_active = sum(1 for v in second_half if v != 0)
            avg_first = np.mean([abs(v) for v in first_half if v != 0]) if first_active > 0 else 0
            if first_active >= 2 and second_active == 0 and avg_first > total_actual * 0.002:
                issues.append({
                    "type": "dormant_account",
                    "account": account_nr,
                    "account_name": account_name,
                    "detail": {
                        "last_active_period": str(grp[grp["actual"] != 0].sort_values("period")["period"].iloc[-1]) if len(grp[grp["actual"] != 0]) > 0 else "?",
                        "avg_when_active": round(avg_first, 0),
                    },
                })

    # 4. TOMMA KONTON MED BUDGET
    # Konton som har budget men noll utfall — kan tyda på att bokföring saknas
    if "budget" in df.columns:
        budget_grp = df.groupby("account").agg(
            total_actual=("actual", "sum"),
            total_budget=("budget", "sum"),
            account_name=("account_name", "first"),
        ).reset_index()
        for _, row in budget_grp.iterrows():
            if abs(float(row["total_budget"])) > total_actual * 0.005 and float(row["total_actual"]) == 0:
                issues.append({
                    "type": "budget_no_actual",
                    "account": str(row["account"]),
                    "account_name": str(row["account_name"]),
                    "detail": {
                        "budget": round(float(row["total_budget"]), 0),
                    },
                })

    # 5. PERIODER UTAN BOKFÖRING
    if len(all_periods) >= 3:
        period_totals = df.groupby("period")["actual"].sum()
        for p in all_periods:
            if p in period_totals.index:
                total = abs(float(period_totals[p]))
                avg = abs(float(period_totals.mean()))
                if avg > 0 and total < avg * 0.1:
                    issues.append({
                        "type": "empty_period",
                        "account": "—",
                        "account_name": "Hela bolaget",
                        "detail": {
                            "period": p,
                            "total": round(total, 0),
                            "expected": round(avg, 0),
                        },
                    })

    return issues


class DataQualityRequest(BaseModel):
    pack: dict


@alerts_router.post("/data-quality")
async def data_quality(req: DataQualityRequest):
    """
    Hittar datakvalitetsproblem och låter AI bedöma vilka som spelar roll.
    """
    pack = req.pack or {}
    issues = detect_data_issues(pack)

    if not issues:
        return {
            "checks": [],
            "summary": "Inga datakvalitetsproblem hittades.",
            "total_issues_found": 0,
        }

    total_actual = abs(float(pack.get("total_actual", 0)))
    current_period = pack.get("current_period", "")

    # ── Bygg AI-prompt ──
    openai_key = os.getenv("OPENAI_API_KEY")

    if openai_key and len(issues) > 0:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=openai_key)

            issue_descriptions = []
            for issue in issues[:30]:  # max 30 till prompten
                t = issue["type"]
                d = issue["detail"]
                acc = f"{issue['account']} {issue['account_name']}"
                if t == "missing_period":
                    issue_descriptions.append(
                        f"SAKNAD PERIOD: {acc} — saknar data för {', '.join(d['missing_periods'])} "
                        f"(finns i {d['present_periods']}/{d['expected_periods']} perioder, snitt {fmt_sek(d['avg_value'])})"
                    )
                elif t == "outlier":
                    issue_descriptions.append(
                        f"OVANLIGT BELOPP: {acc} — {d['period']}: {fmt_sek(d['value'])} "
                        f"(median: {fmt_sek(d['median'])}, {d['deviation_factor']}x standardavvikelse)"
                    )
                elif t == "dormant_account":
                    issue_descriptions.append(
                        f"INAKTIVT KONTO: {acc} — senast aktivt {d['last_active_period']}, "
                        f"snitt {fmt_sek(d['avg_when_active'])} när aktivt, noll sedan dess"
                    )
                elif t == "budget_no_actual":
                    issue_descriptions.append(
                        f"BUDGET UTAN UTFALL: {acc} — budget {fmt_sek(d['budget'])} men 0 kr bokfört"
                    )
                elif t == "empty_period":
                    issue_descriptions.append(
                        f"TOM PERIOD: {d['period']} — bara {fmt_sek(d['total'])} bokfört "
                        f"(normalt {fmt_sek(d['expected'])})"
                    )

            prompt = f"""Du är en erfaren redovisningskonsult som granskar datakvalitet i bokföringen.

BOLAG: Omsättning {fmt_sek(total_actual)}, period {current_period}

POTENTIELLA PROBLEM ({len(issues)} st hittade av systemet):
{chr(10).join(issue_descriptions)}

UPPGIFT:
Välj ut BARA de problem som en controller faktiskt behöver åtgärda.
Ignorera:
- Bagateller (små konton, obetydliga belopp)
- Saker som troligen har naturliga förklaringar (säsongsvariationer etc)
- Konton som inte påverkar resultat- eller balansräkning väsentligt

Returnera BARA JSON:
{{
  "flagged": [
    {{
      "type": "missing_period|outlier|dormant_account|budget_no_actual|empty_period",
      "account": "kontonummer",
      "severity": "critical|warning|info",
      "headline": "Kort rubrik (max 8 ord)",
      "explanation": "1-2 meningar: vad som troligen hänt och varför det spelar roll",
      "suggestion": "Konkret åtgärd"
    }}
  ],
  "overall": "1 mening om datakvaliteten generellt"
}}

Flagga 0-5 problem. Kvalitet > kvantitet."""

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Du är expert på redovisning och datakvalitet. Returnera BARA JSON."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=800,
                temperature=0.1,
            )

            raw = re.sub(r"```json|```", "", resp.choices[0].message.content or "{}").strip()
            ai_result = json.loads(raw)

        except Exception as e:
            print(f"[DataQuality] AI error: {e}")
            ai_result = None
    else:
        ai_result = None

    # ── Bygg response ──
    if ai_result and "flagged" in ai_result:
        checks = []
        for f in ai_result["flagged"]:
            # Hitta matchande issue för drilldown-data
            match = next((
                iss for iss in issues
                if iss["type"] == f.get("type") and str(iss["account"]) == str(f.get("account", ""))
            ), None)

            checks.append({
                "type": f.get("type", ""),
                "account": f.get("account", ""),
                "account_name": match["account_name"] if match else "",
                "severity": f.get("severity", "info"),
                "headline": f.get("headline", ""),
                "explanation": f.get("explanation", ""),
                "suggestion": f.get("suggestion", ""),
                "detail": match["detail"] if match else {},
            })

        return {
            "checks": checks,
            "summary": ai_result.get("overall", ""),
            "total_issues_found": len(issues),
            "ai_flagged": len(checks),
        }

    # ── Fallback utan AI ──
    # Sortera efter typ-prioritet och ta topp 5
    type_priority = {
        "budget_no_actual": 5,
        "empty_period": 4,
        "missing_period": 3,
        "outlier": 2,
        "dormant_account": 1,
    }
    issues.sort(key=lambda i: type_priority.get(i["type"], 0), reverse=True)

    checks = []
    for iss in issues[:5]:
        t = iss["type"]
        d = iss["detail"]
        headlines = {
            "missing_period": f"{iss['account_name']} saknar data i {', '.join(d.get('missing_periods', []))}",
            "outlier": f"Ovanligt belopp på {iss['account_name']}",
            "dormant_account": f"{iss['account_name']} har slutat användas",
            "budget_no_actual": f"{iss['account_name']} har budget men inget utfall",
            "empty_period": f"Period {d.get('period', '?')} saknar bokföring",
        }
        checks.append({
            "type": t,
            "account": iss["account"],
            "account_name": iss["account_name"],
            "severity": "warning" if t in ("budget_no_actual", "empty_period") else "info",
            "headline": headlines.get(t, "Datakvalitetsproblem"),
            "explanation": "",
            "suggestion": "Kontrollera bokföringen.",
            "detail": d,
        })

    return {
        "checks": checks,
        "summary": f"{len(issues)} potentiella problem hittades, {len(checks)} bedöms som väsentliga.",
        "total_issues_found": len(issues),
        "ai_flagged": len(checks),
    }
