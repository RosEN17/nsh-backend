"""
NordSheet — Fortnox Integration Routes
=======================================
Lägg till i main.py:  from fortnox_routes import fortnox_router
                      app.include_router(fortnox_router)

Env-variabler som krävs (Vercel → Settings → Environment Variables):
  FORTNOX_CLIENT_ID        — från Utvecklarportalen → din integration
  FORTNOX_CLIENT_SECRET    — från Utvecklarportalen → din integration
  FORTNOX_REDIRECT_URI     — t.ex. https://app.nordsheet.com/connect?fortnox=callback
  SUPABASE_URL             — er Supabase-URL
  SUPABASE_SERVICE_ROLE_KEY — service role key (ej anon)

Scopes som behövs i Fortnox Utvecklarportalen:
  bookkeeping, companyinformation, costcenter, project, settings
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
import httpx, os, json, base64, time

fortnox_router = APIRouter(prefix="/api/fortnox", tags=["fortnox"])

# ═══════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════

FORTNOX_CLIENT_ID     = os.getenv("FORTNOX_CLIENT_ID", "")
FORTNOX_CLIENT_SECRET = os.getenv("FORTNOX_CLIENT_SECRET", "")
FORTNOX_REDIRECT_URI  = os.getenv("FORTNOX_REDIRECT_URI", "")
FORTNOX_AUTH_URL       = "https://apps.fortnox.se/oauth-v1/auth"
FORTNOX_TOKEN_URL      = "https://apps.fortnox.se/oauth-v1/token"
FORTNOX_API_BASE       = "https://api.fortnox.se/3"

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Scopes vi behöver: kontoplan, bokföring (verif/SIE), bolagsinfo,
# kostnadsställen, projekt, inställningar
FORTNOX_SCOPES = "bookkeeping companyinformation costcenter project settings"


def _get_supabase():
    """Returnerar Supabase-klient om konfigurerad."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        from supabase import create_client
        return create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"[Fortnox] Supabase init error: {e}")
        return None


def _basic_auth_header() -> str:
    """Base64-encodar client_id:client_secret för token-requests."""
    creds = f"{FORTNOX_CLIENT_ID}:{FORTNOX_CLIENT_SECRET}"
    return base64.b64encode(creds.encode()).decode()


async def _fortnox_api_get(endpoint: str, access_token: str, params: dict = None) -> dict:
    """Gör ett GET-anrop mot Fortnox API v3."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FORTNOX_API_BASE}{endpoint}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            params=params or {},
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Fortnox access token har gått ut — anslut igen.")
        if resp.status_code == 429:
            raise HTTPException(status_code=429, detail="Fortnox rate limit — försök igen om en stund.")
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=f"Fortnox API-fel: {resp.text[:300]}")
        return resp.json()


def _save_tokens(company_id: str, tokens: dict):
    """Sparar tokens till Supabase (tabell: fortnox_connections)."""
    sb = _get_supabase()
    if not sb:
        print("[Fortnox] Supabase ej konfigurerad — tokens sparas inte")
        return
    row = {
        "company_id": company_id,
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "token_type": tokens.get("token_type", "bearer"),
        "expires_in": tokens.get("expires_in", 3600),
        "scope": tokens.get("scope", ""),
        "updated_at": "now()",
    }
    try:
        # Upsert — uppdatera om company_id redan finns
        sb.table("fortnox_connections").upsert(row, on_conflict="company_id").execute()
        print(f"[Fortnox] Tokens sparade för company_id={company_id}")
    except Exception as e:
        print(f"[Fortnox] Token save error: {e}")


def _load_tokens(company_id: str) -> Optional[dict]:
    """Hämtar tokens från Supabase."""
    sb = _get_supabase()
    if not sb:
        return None
    try:
        res = sb.table("fortnox_connections").select("*").eq("company_id", company_id).single().execute()
        return res.data if res.data else None
    except Exception:
        return None


async def _refresh_access_token(company_id: str, refresh_token: str) -> Optional[str]:
    """Använder refresh_token för att hämta ny access_token (giltig 1h)."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            FORTNOX_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {_basic_auth_header()}",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    if resp.status_code != 200:
        print(f"[Fortnox] Refresh failed: {resp.status_code} {resp.text[:200]}")
        return None
    tokens = resp.json()
    _save_tokens(company_id, tokens)
    return tokens.get("access_token")


async def _get_valid_token(company_id: str) -> str:
    """Hämtar giltig access token — refreshar automatiskt om behövs."""
    conn = _load_tokens(company_id)
    if not conn:
        raise HTTPException(status_code=401, detail="Fortnox ej ansluten. Gå till Inställningar → Koppla Fortnox.")
    # Försök med befintlig token först, om den misslyckas: refresh
    access_token = conn.get("access_token", "")
    refresh_token = conn.get("refresh_token", "")
    if not access_token and refresh_token:
        access_token = await _refresh_access_token(company_id, refresh_token)
        if not access_token:
            raise HTTPException(status_code=401, detail="Kunde inte förnya Fortnox-token — koppla om.")
    return access_token


# ═══════════════════════════════════════════════════════════════════
# 1) OAuth — Starta auktorisering
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/auth-url")
async def fortnox_auth_url(company_id: str = Query(...)):
    """
    Returnerar URL som frontenden öppnar i webbläsaren.
    Användaren loggar in i Fortnox och godkänner scopesen.
    """
    if not FORTNOX_CLIENT_ID or not FORTNOX_REDIRECT_URI:
        raise HTTPException(status_code=500, detail="FORTNOX_CLIENT_ID / FORTNOX_REDIRECT_URI saknas i env.")

    # state = company_id så vi vet vem som kopplar vid callback
    url = (
        f"{FORTNOX_AUTH_URL}"
        f"?client_id={FORTNOX_CLIENT_ID}"
        f"&redirect_uri={FORTNOX_REDIRECT_URI}"
        f"&scope={FORTNOX_SCOPES}"
        f"&state={company_id}"
        f"&access_type=offline"
        f"&response_type=code"
    )
    return {"url": url}


# ═══════════════════════════════════════════════════════════════════
# 2) OAuth — Callback (byter auth code mot tokens)
# ═══════════════════════════════════════════════════════════════════

class FortnoxCallbackBody(BaseModel):
    code: str
    state: str  # company_id


@fortnox_router.post("/callback")
async def fortnox_callback(body: FortnoxCallbackBody):
    """
    Frontenden skickar auth-koden hit efter redirect.
    Vi byter den mot access_token + refresh_token.
    """
    if not FORTNOX_CLIENT_ID or not FORTNOX_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Fortnox credentials saknas i env.")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            FORTNOX_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {_basic_auth_header()}",
            },
            data={
                "grant_type": "authorization_code",
                "code": body.code,
                "redirect_uri": FORTNOX_REDIRECT_URI,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Fortnox token-byte misslyckades: {resp.text[:300]}",
        )

    tokens = resp.json()
    _save_tokens(body.state, tokens)

    return {"ok": True, "message": "Fortnox kopplad!", "scope": tokens.get("scope", "")}


# ═══════════════════════════════════════════════════════════════════
# 3) Status-check
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/status")
async def fortnox_status(company_id: str = Query(...)):
    """Returnerar om företaget har en aktiv Fortnox-koppling."""
    conn = _load_tokens(company_id)
    if not conn or not conn.get("access_token"):
        return {"connected": False}
    return {
        "connected": True,
        "scope": conn.get("scope", ""),
    }


# ═══════════════════════════════════════════════════════════════════
# 4) Disconnect
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.post("/disconnect")
async def fortnox_disconnect(company_id: str = Query(...)):
    """Tar bort Fortnox-kopplingen."""
    sb = _get_supabase()
    if sb:
        try:
            sb.table("fortnox_connections").delete().eq("company_id", company_id).execute()
        except Exception as e:
            print(f"[Fortnox] Disconnect error: {e}")
    return {"ok": True, "message": "Fortnox frånkopplad."}


# ═══════════════════════════════════════════════════════════════════
# 5) Hämta bolagsinfo
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/company-info")
async def fortnox_company_info(company_id: str = Query(...)):
    """Hämtar bolagsinfo från Fortnox."""
    token = await _get_valid_token(company_id)
    try:
        data = await _fortnox_api_get("/companyinformation", token)
    except HTTPException as e:
        if e.status_code == 401:
            # Försök refresh
            conn = _load_tokens(company_id)
            new_token = await _refresh_access_token(company_id, conn.get("refresh_token", ""))
            if not new_token:
                raise
            data = await _fortnox_api_get("/companyinformation", new_token)
        else:
            raise
    return data.get("CompanyInformation", data)


# ═══════════════════════════════════════════════════════════════════
# 6) Hämta kontoplan
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/accounts")
async def fortnox_accounts(
    company_id: str = Query(...),
    financial_year: Optional[int] = Query(None),
):
    """
    Hämtar kontoplan. Paginerar automatiskt (Fortnox max 100/sida).
    Returnerar alla konton med nummer, beskrivning, SRU, balans.
    """
    token = await _get_valid_token(company_id)
    all_accounts = []
    page = 1

    while True:
        params = {"page": page}
        if financial_year:
            params["financialyear"] = financial_year
        data = await _fortnox_api_get("/accounts", token, params)
        accounts = data.get("Accounts", [])
        all_accounts.extend(accounts)

        meta = data.get("MetaInformation", {})
        total_pages = meta.get("@TotalPages", 1)
        if page >= total_pages:
            break
        page += 1

    return {
        "accounts": all_accounts,
        "total": len(all_accounts),
    }


# ═══════════════════════════════════════════════════════════════════
# 7) Hämta räkenskapsår
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/financial-years")
async def fortnox_financial_years(company_id: str = Query(...)):
    """Hämtar alla räkenskapsår."""
    token = await _get_valid_token(company_id)
    data = await _fortnox_api_get("/financialyears", token)
    return {"financial_years": data.get("FinancialYears", [])}


# ═══════════════════════════════════════════════════════════════════
# 8) Hämta verifikationer (vouchers) för en period
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/vouchers")
async def fortnox_vouchers(
    company_id: str = Query(...),
    financial_year: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
):
    """Hämtar verifikationer med transaktionsrader."""
    token = await _get_valid_token(company_id)
    all_vouchers = []
    page = 1

    while True:
        params = {"page": page}
        if financial_year:
            params["financialyear"] = financial_year
        if from_date:
            params["fromdate"] = from_date
        if to_date:
            params["todate"] = to_date
        data = await _fortnox_api_get("/vouchers", token, params)
        vouchers = data.get("Vouchers", [])
        all_vouchers.extend(vouchers)

        meta = data.get("MetaInformation", {})
        if page >= meta.get("@TotalPages", 1):
            break
        page += 1

    return {"vouchers": all_vouchers, "total": len(all_vouchers)}


# ═══════════════════════════════════════════════════════════════════
# 9) Hämta SIE-export (Resultat- & Balansräkning)
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/sie")
async def fortnox_sie(
    company_id: str = Query(...),
    sie_type: int = Query(4, description="SIE-typ: 1-4, typ 4 = verifikationer"),
    financial_year: Optional[int] = Query(None),
):
    """
    Exporterar SIE-fil från Fortnox.
    SIE-typ 4 innehåller allt: kontoplan, saldon, verifikationer.
    """
    token = await _get_valid_token(company_id)
    params = {"type": sie_type}
    if financial_year:
        params["financialyear"] = financial_year

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{FORTNOX_API_BASE}/sie/{sie_type}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            params=params,
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"SIE-export misslyckades: {resp.text[:300]}")

    return {"sie_data": resp.text}


# ═══════════════════════════════════════════════════════════════════
# 10) HUVUDENDPOINT: Hämta & transformera till NordSheet pack-format
# ═══════════════════════════════════════════════════════════════════

class FortnoxSyncRequest(BaseModel):
    company_id: str
    financial_year: Optional[int] = None
    from_date: Optional[str] = None
    to_date: Optional[str] = None


@fortnox_router.post("/sync")
async def fortnox_sync(req: FortnoxSyncRequest):
    """
    HUVUDFUNKTION — Hämtar data från Fortnox och bygger pack-format.
    Optimerad: parallella voucher-anrop med delad httpx-klient.
    """
    import asyncio

    token = await _get_valid_token(req.company_id)

    # Delad httpx-klient för alla anrop i synken
    async with httpx.AsyncClient(timeout=30, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }) as client:

        async def _api_get(endpoint: str, params: dict = None) -> dict:
            resp = await client.get(
                f"{FORTNOX_API_BASE}{endpoint}",
                params=params or {},
            )
            if resp.status_code == 429:
                # Rate limit — wait and retry once
                await asyncio.sleep(1)
                resp = await client.get(f"{FORTNOX_API_BASE}{endpoint}", params=params or {})
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=f"Fortnox API-fel: {resp.text[:300]}")
            return resp.json()

        # ── 1) Kontoplan ──────────────────────────────────────────────
        accounts_map = {}
        page = 1
        while True:
            params = {"page": page}
            if req.financial_year:
                params["financialyear"] = req.financial_year
            data = await _api_get("/accounts", params)
            for acc in data.get("Accounts", []):
                accounts_map[acc["Number"]] = {
                    "description": acc.get("Description", ""),
                    "sru": acc.get("SRU", 0),
                    "balance_brought": acc.get("BalanceBroughtForward", 0),
                }
            meta = data.get("MetaInformation", {})
            if page >= meta.get("@TotalPages", 1):
                break
            page += 1

        # ── 2) Verifikationer — hämta lista, sen detaljer parallellt ──
        # Först: samla alla voucher-referenser
        voucher_refs = []  # (series, number, period)
        page = 1
        while True:
            params = {"page": page}
            if req.financial_year:
                params["financialyear"] = req.financial_year
            if req.from_date:
                params["fromdate"] = req.from_date
            if req.to_date:
                params["todate"] = req.to_date

            data = await _api_get("/vouchers", params)

            for voucher in data.get("Vouchers", []):
                v_date = voucher.get("TransactionDate", voucher.get("Date", ""))
                period = v_date[:7] if v_date and len(v_date) >= 7 else "Unknown"
                v_series = voucher.get("VoucherSeries", "")
                v_number = voucher.get("VoucherNumber", "")
                if v_series and v_number is not None:
                    voucher_refs.append((v_series, v_number, period))

            meta = data.get("MetaInformation", {})
            if page >= meta.get("@TotalPages", 1):
                break
            page += 1

        # Sen: hämta detaljer parallellt i batchar om 15
        rows = []
        BATCH_SIZE = 15  # Fortnox rate limit-vänligt

        async def _fetch_voucher_detail(series: str, number: int, period: str) -> list:
            try:
                fy_param = f"?financialyear={req.financial_year}" if req.financial_year else ""
                detail = await _api_get(f"/vouchers/{series}/{number}{fy_param}")
                result = []
                for row in detail.get("Voucher", {}).get("VoucherRows", []):
                    acc_nr = row.get("Account", 0)
                    result.append({
                        "period": period,
                        "account": str(acc_nr),
                        "account_name": accounts_map.get(acc_nr, {}).get("description", ""),
                        "actual": float(row.get("Debit", 0) or 0) - float(row.get("Credit", 0) or 0),
                        "cost_center": row.get("CostCenter", ""),
                        "project": row.get("Project", ""),
                    })
                return result
            except Exception as e:
                print(f"[Fortnox] Voucher detail error {series}-{number}: {e}")
                return []

        # Process in batches
        for i in range(0, len(voucher_refs), BATCH_SIZE):
            batch = voucher_refs[i:i + BATCH_SIZE]
            results = await asyncio.gather(*[
                _fetch_voucher_detail(s, n, p) for s, n, p in batch
            ])
            for result_rows in results:
                rows.extend(result_rows)

    if not rows:
        return {
            "pack": None,
            "message": "Inga verifikationer hittades för vald period.",
            "accounts_count": len(accounts_map),
        }

    # ── 3) Aggregera till pack-format ─────────────────────────────
    import pandas as pd
    df = pd.DataFrame(rows)

    # Aggregera per konto+period
    agg = df.groupby(["period", "account", "account_name"]).agg(
        actual=("actual", "sum")
    ).reset_index()

    # Total per konto (alla perioder)
    by_account = df.groupby(["account", "account_name"]).agg(
        actual=("actual", "sum")
    ).reset_index()

    # Perioder
    periods = sorted(df["period"].unique().tolist())
    current_period = periods[-1] if periods else "Unknown"
    previous_period = periods[-2] if len(periods) >= 2 else None

    # Filtrera till resultaträkningskonton (3xxx-8xxx) för meningsfull summering
    # Balansräkningskonton (1xxx-2xxx) tar ut varandra i dubbel bokföring
    def is_income_expense(account_nr: str) -> bool:
        try:
            n = int(str(account_nr).strip()[:1])
            return n >= 3  # 3xxx=intäkter, 4-7xxx=kostnader, 8xxx=finansiella
        except Exception:
            return True

    df_result = df[df["account"].apply(is_income_expense)]

    # Total actual = resultaträkning current period
    cur_period_rows = df_result[df_result["period"] == current_period]
    total_actual = float(cur_period_rows["actual"].sum()) if not cur_period_rows.empty else 0.0

    prev_period_rows = df_result[df_result["period"] == previous_period] if previous_period else None
    prev_total_actual = float(prev_period_rows["actual"].sum()) if prev_period_rows is not None and not prev_period_rows.empty else 0.0

    # Period series — bara resultaträkning per period
    period_series = []
    for p in periods:
        p_df  = df_result[df_result["period"] == p]
        p_sum = float(p_df["actual"].sum())
        period_series.append({"period": p, "actual": round(p_sum, 0), "budget": 0})

    # MoM
    mom_diff, mom_pct = None, None
    if previous_period and prev_total_actual != 0:
        mom_diff = total_actual - prev_total_actual
        mom_pct  = mom_diff / abs(prev_total_actual)

    # ── 4) Smarter variance detection med MoM-jämförelse ────────
    # Bygg per-period lookup för trenddetektion
    by_period_account = {}
    for _, row in agg.iterrows():
        key = (str(row["period"]), str(row["account"]))
        by_period_account[key] = float(row["actual"])

    # by_account for current period only (for dashboard display)
    cur_by_account = df[df["period"] == current_period].groupby(
        ["account", "account_name"]
    ).agg(actual=("actual", "sum")).reset_index()

    # Räkna ut MoM-förändringar per konto
    MIN_ABS   = 10_000   # 10 tkr minimum för att flagga
    MIN_MOM   = 0.15     # 15% MoM-förändring

    all_flagged = []
    top_budget  = []
    top_mom     = []

    for _, r in cur_by_account.iterrows() if not cur_by_account.empty else by_account.iterrows():
        konto      = str(r["account"])
        label      = r["account_name"] or konto
        actual_cur = float(r["actual"])

        # MoM-trend: jämför current vs previous period
        prev_val = by_period_account.get((previous_period, konto), None) if previous_period else None
        cur_val  = by_period_account.get((current_period, konto), None)

        mom_diff_acc = None
        mom_pct_acc  = None
        if prev_val is not None and cur_val is not None and prev_val != 0:
            mom_diff_acc = cur_val - prev_val
            mom_pct_acc  = mom_diff_acc / abs(prev_val)

        # Trend: hur många perioder i rad har kontot rört sig åt samma håll?
        account_history = []
        for p in periods[-6:]:
            v = by_period_account.get((p, konto))
            if v is not None:
                account_history.append(v)

        trend_direction = None
        consecutive_periods = 0
        if len(account_history) >= 3:
            diffs = [account_history[i] - account_history[i-1] for i in range(1, len(account_history))]
            if all(d > 0 for d in diffs[-2:]):
                trend_direction = "stigande"
                consecutive_periods = sum(1 for d in reversed(diffs) if d > 0)
            elif all(d < 0 for d in diffs[-2:]):
                trend_direction = "sjunkande"
                consecutive_periods = sum(1 for d in reversed(diffs) if d < 0)

        # Är detta en flaggningsvärd avvikelse?
        is_flagged = False
        flag_reason = []

        if mom_diff_acc is not None and abs(mom_diff_acc) >= MIN_ABS and abs(mom_pct_acc or 0) >= MIN_MOM:
            is_flagged = True
            flag_reason.append(f"MoM {'+' if mom_diff_acc > 0 else ''}{mom_diff_acc:,.0f} kr ({(mom_pct_acc or 0)*100:+.1f}%)")

        if consecutive_periods >= 3:
            is_flagged = True
            flag_reason.append(f"{trend_direction} {consecutive_periods} perioder i rad")

        # Klassificera typ baserat på mönster
        var_type = "Okänd"
        if consecutive_periods >= 3:
            var_type = "Strukturell"
        elif consecutive_periods >= 2:
            var_type = "Återkommande"
        elif mom_diff_acc is not None and abs(mom_diff_acc) >= MIN_ABS:
            var_type = "Engång"

        row_dict = {
            "Konto":          konto,
            "Label":          label,
            "Utfall":         round(actual_cur, 0),
            "Budget":         0,
            "Vs budget diff": round(mom_diff_acc or 0, 0),
            "Vs budget %":    round(mom_pct_acc or 0, 4),
            "MoM diff":       round(mom_diff_acc or 0, 0),
            "MoM %":          round(mom_pct_acc or 0, 4),
            "trend_direction":      trend_direction,
            "consecutive_periods":  consecutive_periods,
            "flag_reason":          " | ".join(flag_reason),
            "variance_type":        var_type,
            "account_history":      account_history,
        }

        if is_flagged:
            all_flagged.append(row_dict)
            if (mom_diff_acc or 0) < 0 or (actual_cur < 0 and trend_direction == "sjunkande"):
                top_budget.append(row_dict)
            else:
                top_mom.append(row_dict)

    # Sortera: störst absolut avvikelse först
    top_budget.sort(key=lambda x: abs(x.get("MoM diff", 0)), reverse=True)
    top_mom.sort(key=lambda x: abs(x.get("MoM diff", 0)), reverse=True)
    top_budget = top_budget[:10]
    top_mom    = top_mom[:10]

    kpi_summary = {
        "Nu":          round(total_actual, 0),
        "Föregående":  round(prev_total_actual, 0) if previous_period else None,
        "MoM diff":    round(mom_diff, 0) if mom_diff is not None else None,
        "MoM %":       round(mom_pct, 4) if mom_pct is not None else None,
        "Budget":      0,
        "Vs budget diff": 0,
        "Vs budget %": 0,
    }

    pack = {
        "source":          "fortnox",
        "current_period":  current_period,
        "previous_period": previous_period,
        "periods":         periods,
        "warnings":        ["Budget-data saknas — avvikelser baseras på MoM-jämförelse."] if not req.from_date else [],
        "narrative":       f"Period {current_period}: Utfall {total_actual:,.0f} SEK. {len(all_flagged)} konton med MoM-avvikelse över 15%.",
        "top_budget":      top_budget,
        "top_mom":         top_mom,
        "all_flagged":     all_flagged,
        "kpi_summary":     [kpi_summary],
        "total_actual":    round(total_actual, 0),
        "total_budget":    0,
        "period_series":   period_series,
        "account_rows":    agg.rename(columns={
            "account":      "Konto",
            "account_name": "Label",
            "actual":       "Utfall",
            "period":       "period",
        }).to_dict(orient="records"),
        "detailed_rows":   agg[agg["account"].apply(is_income_expense)].to_dict(orient="records"),
    }

    return {
        "pack": pack,
        "message": f"Hämtade {len(rows)} transaktioner från {len(periods)} perioder.",
        "accounts_count": len(accounts_map),
        "periods": periods,
    }


# ═══════════════════════════════════════════════════════════════════
# 11) Kostnadsställen
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/cost-centers")
async def fortnox_cost_centers(company_id: str = Query(...)):
    """Hämtar alla kostnadsställen."""
    token = await _get_valid_token(company_id)
    data = await _fortnox_api_get("/costcenters", token)
    return {"cost_centers": data.get("CostCenters", [])}


# ═══════════════════════════════════════════════════════════════════
# 12) Projekt
# ═══════════════════════════════════════════════════════════════════

@fortnox_router.get("/projects")
async def fortnox_projects(company_id: str = Query(...)):
    """Hämtar alla projekt."""
    token = await _get_valid_token(company_id)
    data = await _fortnox_api_get("/projects", token)
    return {"projects": data.get("Projects", [])}
