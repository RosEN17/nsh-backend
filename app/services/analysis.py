from __future__ import annotations

import hashlib
import io
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd


MISSING_STRINGS = {"n/a", "na", "n.a.", "null", "none", "nan", "-", "--", ""}
SV_MONTHS = {
    "januari": "01", "jan": "01",
    "februari": "02", "feb": "02",
    "mars": "03", "mar": "03",
    "april": "04", "apr": "04",
    "maj": "05",
    "juni": "06", "jun": "06",
    "juli": "07", "jul": "07",
    "augusti": "08", "aug": "08",
    "september": "09", "sep": "09",
    "oktober": "10", "okt": "10",
    "november": "11", "nov": "11",
    "december": "12", "dec": "12",
}


@dataclass
class Mapping:
    period: str
    account: str
    actual: str
    budget: Optional[str] = None
    entity: Optional[str] = None
    cost_center: Optional[str] = None
    project: Optional[str] = None
    account_name: Optional[str] = None


@dataclass
class VariancePack:
    current_period: str
    previous_period: Optional[str]
    kpi_summary: pd.DataFrame
    top_mom: pd.DataFrame
    top_budget: pd.DataFrame
    drivers_account_mom: pd.DataFrame
    drivers_account_budget: pd.DataFrame
    narrative: str
    data_model_ok: bool
    warnings: List[str]
    model_df: pd.DataFrame


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def norm_str(x) -> str:
    return re.sub(r"\s+", " ", str(x or "").replace("\u00A0", " ").strip())


def is_missing(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except Exception:
        pass

    if isinstance(v, str):
        s = norm_str(v).strip()
        if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
            s = s[1:-1].strip()
        s = s.lower()
        return s in MISSING_STRINGS

    return False


def safe_div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    try:
        return a / b
    except Exception:
        return None


def fmt_money(x) -> str:
    try:
        if x is None or (isinstance(x, float) and pd.isna(x)):
            return ""
        x = float(x)
    except Exception:
        return str(x)

    ax = abs(x)
    if ax >= 1_000_000:
        return f"{x/1_000_000:,.2f} MSEK".replace(",", " ").replace(".", ",")
    if ax >= 1_000:
        return f"{x:,.0f} SEK".replace(",", " ")
    return f"{x:.0f} SEK"


def fmt_pct(x) -> str:
    try:
        if x is None or pd.isna(x):
            return ""
        return f"{x * 100:.0f}%"
    except Exception:
        return ""


def extract_requested_period(user_prompt: str, available_periods: List[str]) -> Optional[str]:
    text = (user_prompt or "").lower().strip()

    m = re.search(r"\b(20\d{2})[-/](0[1-9]|1[0-2])\b", text)
    if m:
        period = f"{m.group(1)}-{m.group(2)}"
        return period if period in available_periods else None

    m = re.search(
        r"\b(januari|jan|februari|feb|mars|mar|april|apr|maj|juni|jun|juli|jul|augusti|aug|september|sep|oktober|okt|november|nov|december|dec)\s+(20\d{2})\b",
        text,
    )
    if m:
        month = SV_MONTHS[m.group(1)]
        period = f"{m.group(2)}-{month}"
        return period if period in available_periods else None

    return None


def to_month_period(series: pd.Series) -> pd.Series:
    """
    Original appens stil: robust normalisering till YYYY-MM.
    Klarar datumkolumner, vanliga datumsträngar, YYYYMM och YYYYMMDD.
    """
    if series is None:
        return pd.Series(dtype="object")

    if pd.api.types.is_datetime64_any_dtype(series):
        return series.dt.to_period("M").astype(str)

    s = series.astype(str).str.replace("\u00A0", " ", regex=False).str.strip()
    s_low = s.str.lower()
    missing_tokens = {"", "-", "--", "null", "none", "nan", "n/a", "na"}
    s = s.where(~s_low.isin(missing_tokens))

    parsed = pd.to_datetime(s, errors="coerce", dayfirst=True)
    out = parsed.dt.to_period("M").astype(str)

    m6 = out.isna() & s.str.match(r"^\d{6}$", na=False)
    if m6.any():
        p2 = pd.to_datetime(s.where(m6), errors="coerce", format="%Y%m")
        out = out.where(~m6, p2.dt.to_period("M").astype(str))

    m8 = out.isna() & s.str.match(r"^\d{8}$", na=False)
    if m8.any():
        p3 = pd.to_datetime(s.where(m8), errors="coerce", format="%Y%m%d")
        out = out.where(~m8, p3.dt.to_period("M").astype(str))

    return out


def month_add(period_yyyy_mm: str, delta_months: int) -> Optional[str]:
    try:
        y, m = [int(x) for x in period_yyyy_mm.split("-")]
        m2 = m + delta_months
        y2 = y + (m2 - 1) // 12
        m2 = (m2 - 1) % 12 + 1
        return f"{y2:04d}-{m2:02d}"
    except Exception:
        return None


def read_upload(file) -> Tuple[pd.DataFrame, bytes]:
    """
    Fungerar både med Streamlit-liknande file-objekt och FastAPI UploadFile.
    """
    try:
        b = file.getvalue()
    except Exception:
        try:
            b = file.read()
        except Exception:
            b = file.read()

    file_name = getattr(file, "name", None) or getattr(file, "filename", "")
    ext = os.path.splitext(file_name)[1].lower()

    if ext == ".csv":
        df = pd.read_csv(
            io.BytesIO(b),
            keep_default_na=True,
            na_values=["N/A", "NA", "n/a", "na", "NULL", "null", "None", "none", "NaN", "nan", "-", "--", ""],
        )
    elif ext in {".xlsx", ".xls"}:
        df = pd.read_excel(
            io.BytesIO(b),
            keep_default_na=True,
            na_values=["N/A", "NA", "n/a", "na", "NULL", "null", "None", "none", "NaN", "nan", "-", "--", ""],
        )
    else:
        raise ValueError("Endast CSV, XLSX och XLS stöds.")

    return df, b


def infer_candidate_columns(df: pd.DataFrame) -> Dict[str, List[str]]:
    """
    Samma idé som originalet: keyword-score + numerisk bonus för actual/budget.
    """
    cols = list(df.columns)

    def score(col: str, keywords: List[str]) -> int:
        cl = str(col).lower()
        return sum(1 for k in keywords if k in cl)

    numeric = {c for c in cols if pd.api.types.is_numeric_dtype(df[c])}
    suggestions: Dict[str, List[str]] = {}

    kw_period = ["månad", "manad", "month", "period", "datum", "date", "year-month", "år-månad", "ar-manad"]
    suggestions["period"] = sorted(cols, key=lambda c: score(c, kw_period), reverse=True)

    kw_account = ["konto", "account", "gl", "ledger", "kontonr", "kontonummer"]
    suggestions["account"] = sorted(cols, key=lambda c: score(c, kw_account), reverse=True)

    kw_actual = ["utfall", "actual", "belopp", "amount", "sum", "value", "kostnad", "intäkt", "intakt"]
    suggestions["actual"] = sorted(
        cols,
        key=lambda c: (score(c, kw_actual) + (2 if c in numeric else 0)),
        reverse=True,
    )

    kw_budget = ["budget", "bud", "plan"]
    suggestions["budget"] = sorted(
        cols,
        key=lambda c: (score(c, kw_budget) + (2 if c in numeric else 0)),
        reverse=True,
    )

    kw_entity = ["bolag", "entity", "company", "enhet", "land", "country"]
    suggestions["entity"] = sorted(cols, key=lambda c: score(c, kw_entity), reverse=True)

    kw_cc = ["kostnadsställe", "kostnadsstalle", "cost center", "costcenter", "cc", "resultatenhet", "res"]
    suggestions["cost_center"] = sorted(cols, key=lambda c: score(c, kw_cc), reverse=True)

    kw_proj = ["projekt", "project"]
    suggestions["project"] = sorted(cols, key=lambda c: score(c, kw_proj), reverse=True)

    return suggestions


def choose_auto_mapping(df: pd.DataFrame) -> Mapping:
    suggestions = infer_candidate_columns(df)
    cols = list(df.columns)

    def first_valid(candidates: List[str], require_numeric: bool = False) -> Optional[str]:
        for c in candidates:
            if c in cols and str(c).strip():
                if require_numeric and not pd.api.types.is_numeric_dtype(df[c]):
                    continue
                return c
        return None

    period_col = first_valid(suggestions["period"])
    account_col = first_valid(suggestions["account"])
    actual_col = first_valid(suggestions["actual"], require_numeric=True) or first_valid(suggestions["actual"])
    budget_col = first_valid(suggestions["budget"], require_numeric=True) or first_valid(suggestions["budget"])
    entity_col = first_valid(suggestions["entity"])
    cc_col = first_valid(suggestions["cost_center"])
    project_col = first_valid(suggestions["project"])

    name_candidates = [c for c in cols if "namn" in str(c).lower() or "name" in str(c).lower()]
    acc_name_col = name_candidates[0] if name_candidates else None

    if not period_col or not account_col or not actual_col:
        raise ValueError("Kunde inte hitta tillräckligt bra kolumner automatiskt.")

    return Mapping(
        period=period_col,
        account=account_col,
        actual=actual_col,
        budget=budget_col,
        entity=entity_col,
        cost_center=cc_col,
        project=project_col,
        account_name=acc_name_col,
    )


def build_model_df(df: pd.DataFrame, m: Mapping) -> pd.DataFrame:
    """
    Behåller originalets idé med __-kolumner som standardiserat modellager.
    """
    out = df.copy()

    parsed = to_month_period(out[m.period])

    if "__period_manual" in out.columns:
        manual = out["__period_manual"].astype(str).str.replace("\u00A0", " ", regex=False).str.strip()
        manual = manual.where(manual.ne(""), pd.NA)
        out["__period"] = manual.fillna(parsed)
    else:
        out["__period"] = parsed

    out["__actual"] = pd.to_numeric(out[m.actual], errors="coerce")
    out["__budget"] = (
        pd.to_numeric(out[m.budget], errors="coerce")
        if (m.budget and m.budget in out.columns)
        else pd.Series([pd.NA] * len(out), index=out.index)
    )
    out["__account"] = out[m.account].astype(str).str.strip()
    out["__acc_name"] = out[m.account_name].astype(str).str.strip() if (m.account_name and m.account_name in out.columns) else ""
    out["__entity"] = out[m.entity].astype(str).str.strip() if (m.entity and m.entity in out.columns) else ""
    out["__cc"] = out[m.cost_center].astype(str).str.strip() if (m.cost_center and m.cost_center in out.columns) else ""
    out["__project"] = out[m.project].astype(str).str.strip() if (m.project and m.project in out.columns) else ""

    out = out[~out["__period"].isna()].copy()
    out = out[~out["__account"].astype(str).str.strip().eq("")].copy()
    out["__actual"] = out["__actual"].fillna(0.0)

    return out.reset_index(drop=True)


def _kpi_table(current: float, prev: Optional[float], budget: Optional[float]) -> pd.DataFrame:
    mom_diff = (current - prev) if prev is not None else None
    mom_pct = safe_div(mom_diff, prev) if prev is not None else None

    bud_diff = (current - budget) if budget is not None else None
    bud_pct = safe_div(bud_diff, budget) if budget is not None else None

    rows = [
        {
            "KPI": "Utfall (Actual)",
            "Nu": current,
            "Föregående": prev,
            "MoM diff": mom_diff,
            "MoM %": mom_pct,
            "Budget": budget,
            "Vs budget diff": bud_diff,
            "Vs budget %": bud_pct,
        }
    ]
    return pd.DataFrame(rows)


def _label_column(df: pd.DataFrame) -> pd.Series:
    if "__acc_name" in df.columns:
        name = df["__acc_name"].fillna("").astype(str).str.strip()
        acc = df["__account"].fillna("").astype(str).str.strip()
        return name.where(name.ne(""), acc)
    return df["__account"].fillna("").astype(str).str.strip()


def _aggregate_period(model_df: pd.DataFrame, period: str) -> pd.DataFrame:
    d = model_df.loc[model_df["__period"] == period].copy()
    if d.empty:
        return pd.DataFrame(columns=["Konto", "Label", "Utfall", "Budget"])

    d["Label"] = _label_column(d)

    agg = (
        d.groupby(["__account", "Label"], dropna=False, as_index=False)
        .agg(
            Utfall=("__actual", "sum"),
            Budget=("__budget", "sum"),
        )
        .rename(columns={"__account": "Konto"})
    )

    return agg


def _top_abs(df: pd.DataFrame, diff_col: str, n: int = 10) -> pd.DataFrame:
    if df.empty or diff_col not in df.columns:
        return df.head(0).copy()
    out = df.copy()
    out["_abs_sort"] = out[diff_col].fillna(0).abs()
    out = out.sort_values("_abs_sort", ascending=False).drop(columns="_abs_sort")
    return out.head(n).reset_index(drop=True)


def _build_narrative(
    current_period: str,
    previous_period: Optional[str],
    cur_total: float,
    prev_total: Optional[float],
    bud_total: Optional[float],
    top_mom: pd.DataFrame,
    top_budget: pd.DataFrame,
) -> str:
    parts: List[str] = [f"Aktuell period är {current_period}."]

    if previous_period:
        mom_diff = cur_total - prev_total if prev_total is not None else None
        mom_pct = safe_div(mom_diff, prev_total) if prev_total is not None else None
        parts.append(
            f"Jämfört med {previous_period} är utfallet {fmt_money(cur_total)}"
            + (f", förändring {fmt_money(mom_diff)} ({fmt_pct(mom_pct)})." if mom_diff is not None else ".")
        )
    else:
        parts.append("Ingen föregående period finns tillgänglig för MoM-jämförelse.")

    if bud_total is not None and not pd.isna(bud_total):
        bud_diff = cur_total - bud_total
        bud_pct = safe_div(bud_diff, bud_total)
        parts.append(
            f"Mot budget är avvikelsen {fmt_money(bud_diff)} ({fmt_pct(bud_pct)})."
        )

    if not top_mom.empty:
        first = top_mom.iloc[0]
        parts.append(
            f"Största MoM-drivern är {first.get('Label', first.get('Konto', 'okänt konto'))} "
            f"med {fmt_money(first.get('MoM diff'))}."
        )

    if not top_budget.empty:
        first = top_budget.iloc[0]
        parts.append(
            f"Största budgetavvikelsen ligger på {first.get('Label', first.get('Konto', 'okänt konto'))} "
            f"med {fmt_money(first.get('Vs budget diff'))}."
        )

    return " ".join(parts)


def compute_variances(model_df: pd.DataFrame) -> VariancePack:
    """
    Backend-version som matchar den gamla appens objektmodell och dashboard-behov.
    """
    warnings: List[str] = []

    if model_df is None or model_df.empty:
        return VariancePack(
            current_period="—",
            previous_period=None,
            kpi_summary=pd.DataFrame(),
            top_mom=pd.DataFrame(),
            top_budget=pd.DataFrame(),
            drivers_account_mom=pd.DataFrame(),
            drivers_account_budget=pd.DataFrame(),
            narrative="Ingen data hittades efter modellbygget.",
            data_model_ok=False,
            warnings=["Ingen användbar data hittades."],
            model_df=pd.DataFrame() if model_df is None else model_df,
        )

    periods = sorted([p for p in model_df["__period"].dropna().astype(str).unique().tolist() if str(p).strip()])
    if not periods:
        return VariancePack(
            current_period="—",
            previous_period=None,
            kpi_summary=pd.DataFrame(),
            top_mom=pd.DataFrame(),
            top_budget=pd.DataFrame(),
            drivers_account_mom=pd.DataFrame(),
            drivers_account_budget=pd.DataFrame(),
            narrative="Kunde inte läsa periodkolumnen.",
            data_model_ok=False,
            warnings=["Periodkolumnen kunde inte normaliseras."],
            model_df=model_df,
        )

    current_period = periods[-1]
    previous_period = periods[-2] if len(periods) >= 2 else None

    cur = _aggregate_period(model_df, current_period)
    prev = _aggregate_period(model_df, previous_period) if previous_period else pd.DataFrame(columns=["Konto", "Label", "Utfall"])

    cur_total = float(cur["Utfall"].sum()) if not cur.empty else 0.0
    prev_total = float(prev["Utfall"].sum()) if previous_period and not prev.empty else (0.0 if previous_period else None)

    budget_available = ("Budget" in cur.columns) and cur["Budget"].notna().any()
    bud_total = float(cur["Budget"].fillna(0).sum()) if budget_available else None
    if not budget_available:
        warnings.append("Budgetkolumn saknas eller innehåller inga användbara värden.")

    kpi_summary = _kpi_table(cur_total, prev_total, bud_total)

    merged = cur.merge(
        prev[["Konto", "Utfall"]].rename(columns={"Utfall": "Föregående"}),
        on="Konto",
        how="left",
    )
    merged["Föregående"] = merged["Föregående"].fillna(0.0)
    merged["MoM diff"] = merged["Utfall"] - merged["Föregående"]
    merged["MoM %"] = merged.apply(lambda r: safe_div(r["MoM diff"], r["Föregående"]), axis=1)

    if "Budget" not in merged.columns:
        merged["Budget"] = pd.NA
    merged["Vs budget diff"] = merged["Utfall"] - merged["Budget"]
    merged["Vs budget %"] = merged.apply(
        lambda r: safe_div(r["Vs budget diff"], r["Budget"]) if pd.notna(r["Budget"]) else None,
        axis=1,
    )

    if previous_period is None:
        warnings.append("Det finns ingen föregående period för MoM-jämförelse.")

    top_mom = _top_abs(merged[["Konto", "Label", "Utfall", "Föregående", "MoM diff", "MoM %"]], "MoM diff", n=10)
    top_budget = _top_abs(merged[["Konto", "Label", "Utfall", "Budget", "Vs budget diff", "Vs budget %"]], "Vs budget diff", n=10)

    drivers_account_mom = top_mom.copy()
    drivers_account_budget = top_budget.copy()

    narrative = _build_narrative(
        current_period=current_period,
        previous_period=previous_period,
        cur_total=cur_total,
        prev_total=prev_total,
        bud_total=bud_total,
        top_mom=top_mom,
        top_budget=top_budget,
    )

    return VariancePack(
        current_period=current_period,
        previous_period=previous_period,
        kpi_summary=kpi_summary,
        top_mom=top_mom,
        top_budget=top_budget,
        drivers_account_mom=drivers_account_mom,
        drivers_account_budget=drivers_account_budget,
        narrative=narrative,
        data_model_ok=True,
        warnings=warnings,
        model_df=model_df,
    )


def _get_total_metrics(pack: VariancePack) -> Tuple[float, Optional[float], Optional[float], Optional[float], Optional[float]]:
    """
    Dashboarden i gamla appen använder:
    cur_total, mom_diff, mom_pct, bud_diff, bud_pct = _get_total_metrics(pack)
    """
    if pack is None or pack.kpi_summary is None or pack.kpi_summary.empty:
        return 0.0, None, None, None, None

    row = pack.kpi_summary.iloc[0]
    cur_total = float(row.get("Nu", 0.0)) if pd.notna(row.get("Nu", 0.0)) else 0.0
    mom_diff = row.get("MoM diff")
    mom_pct = row.get("MoM %")
    bud_diff = row.get("Vs budget diff")
    bud_pct = row.get("Vs budget %")

    return (
        cur_total,
        None if pd.isna(mom_diff) else float(mom_diff),
        None if pd.isna(mom_pct) else float(mom_pct),
        None if pd.isna(bud_diff) else float(bud_diff),
        None if pd.isna(bud_pct) else float(bud_pct),
    )


def _traffic_light(pack: VariancePack) -> Tuple[str, str, str]:
    """
    Enkel backend-version för dashboard status.
    """
    _, _, mom_pct, bud_diff, bud_pct = _get_total_metrics(pack)

    if bud_diff is None and mom_pct is None:
        return "Insufficient data", "Ingen budget eller jämförelseperiod tillgänglig.", "neutral"

    if bud_pct is not None:
        if bud_pct <= -0.05:
            return "At risk", "Negativ budgetavvikelse större än 5%.", "bad"
        if bud_pct < 0:
            return "Watch", "Svagt negativ budgetavvikelse.", "watch"

    if mom_pct is not None and mom_pct >= 0:
        return "Healthy", "Utvecklingen mot föregående period är positiv.", "good"

    return "Stable", "Inga kritiska signaler identifierade.", "neutral"


def build_issues(df: pd.DataFrame, mapping: Mapping, file_hash: str) -> List[Dict[str, str]]:
    """
    Grundläggande datakvalitetskontroller för connect/dashboard.
    """
    issues: List[Dict[str, str]] = []

    required = {
        "period": mapping.period,
        "account": mapping.account,
        "actual": mapping.actual,
    }

    for label, col in required.items():
        if not col or col not in df.columns:
            issues.append(
                {
                    "severity": "high",
                    "message": f"Saknar obligatorisk kolumn för {label}: {col}",
                }
            )

    if mapping.budget and mapping.budget not in df.columns:
        issues.append(
            {
                "severity": "medium",
                "message": f"Budgetkolumnen '{mapping.budget}' hittades inte.",
            }
        )

    if mapping.period in df.columns:
        parsed = to_month_period(df[mapping.period])
        bad_periods = int(parsed.isna().sum())
        if bad_periods > 0:
            issues.append(
                {
                    "severity": "medium",
                    "message": f"{bad_periods} rad(er) kunde inte tolkas som period.",
                }
            )

    if mapping.actual in df.columns:
        actual_num = pd.to_numeric(df[mapping.actual], errors="coerce")
        bad_actual = int(actual_num.isna().sum())
        if bad_actual > 0:
            issues.append(
                {
                    "severity": "medium",
                    "message": f"{bad_actual} rad(er) i actual kunde inte tolkas numeriskt.",
                }
            )

    issues.append(
        {
            "severity": "info",
            "message": f"Filhash: {file_hash[:12]}...",
        }
    )

    return issues


def build_variance_split(df: pd.DataFrame, diff_col: str) -> Dict[str, pd.DataFrame]:
    """
    Hjälper variances-sidan att dela upp positiva/negativa drivare.
    """
    if df is None or df.empty or diff_col not in df.columns:
        empty = pd.DataFrame()
        return {"negative": empty, "positive": empty}

    work = df.copy()
    work[diff_col] = pd.to_numeric(work[diff_col], errors="coerce")

    negative = work[work[diff_col] < 0].copy().sort_values(diff_col, ascending=True).reset_index(drop=True)
    positive = work[work[diff_col] > 0].copy().sort_values(diff_col, ascending=False).reset_index(drop=True)

    return {"negative": negative, "positive": positive}


def build_run_rate_forecast(pack: VariancePack, months_forward: int = 3) -> pd.DataFrame:
    """
    Enkel forecast baserad på current actual som run-rate.
    """
    if pack is None or pack.model_df is None or pack.model_df.empty:
        return pd.DataFrame(columns=["Period", "Forecast"])

    cur_period = pack.current_period
    cur_df = pack.model_df.loc[pack.model_df["__period"] == cur_period].copy()
    current_total = float(cur_df["__actual"].sum()) if not cur_df.empty else 0.0

    rows = []
    for i in range(1, months_forward + 1):
        p = month_add(cur_period, i)
        rows.append({"Period": p, "Forecast": current_total})

    return pd.DataFrame(rows)


def pack_to_dict(pack: VariancePack) -> Dict[str, Any]:
    return {
        "current_period": pack.current_period,
        "previous_period": pack.previous_period,
        "kpi_summary": pack.kpi_summary.to_dict(orient="records"),
        "top_mom": pack.top_mom.to_dict(orient="records"),
        "top_budget": pack.top_budget.to_dict(orient="records"),
        "drivers_account_mom": pack.drivers_account_mom.to_dict(orient="records"),
        "drivers_account_budget": pack.drivers_account_budget.to_dict(orient="records"),
        "narrative": pack.narrative,
        "data_model_ok": pack.data_model_ok,
        "warnings": pack.warnings,
    }


def dict_to_pack(data: Dict[str, Any]) -> VariancePack:
    return VariancePack(
        current_period=data.get("current_period", "—"),
        previous_period=data.get("previous_period"),
        kpi_summary=pd.DataFrame(data.get("kpi_summary", [])),
        top_mom=pd.DataFrame(data.get("top_mom", [])),
        top_budget=pd.DataFrame(data.get("top_budget", [])),
        drivers_account_mom=pd.DataFrame(data.get("drivers_account_mom", [])),
        drivers_account_budget=pd.DataFrame(data.get("drivers_account_budget", [])),
        narrative=data.get("narrative", ""),
        data_model_ok=bool(data.get("data_model_ok", False)),
        warnings=list(data.get("warnings", [])),
        model_df=pd.DataFrame(data.get("model_df", [])),
    )
