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
    HUVUDFUNKTION — Hämtar data från Fortnox och bygger pack-format
    som NordSheet dashboard + variansanalys förstår.

    Flöde:
    1. Hämta kontoplan (konto-nr → kontonamn)
    2. Hämta verifikationer med transaktionsrader
    3. Aggregera per konto + period → utfall
    4. Returnera i pack-format (samma som CSV-upload ger)
    """
    token = await _get_valid_token(req.company_id)

    # ── 1) Kontoplan ──────────────────────────────────────────────
    accounts_map = {}
    page = 1
    while True:
        params = {"page": page}
        if req.financial_year:
            params["financialyear"] = req.financial_year
        data = await _fortnox_api_get("/accounts", token, params)
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

    # ── 2) Verifikationer med transaktionsrader ───────────────────
    # Vi hämtar alla vouchers och bygger en DataFrame-liknande struktur
    rows = []
    page = 1
    while True:
        params = {"page": page}
        if req.financial_year:
            params["financialyear"] = req.financial_year
        if req.from_date:
            params["fromdate"] = req.from_date
        if req.to_date:
            params["todate"] = req.to_date

        data = await _fortnox_api_get("/vouchers", token, params)

        for voucher in data.get("Vouchers", []):
            v_date = voucher.get("TransactionDate", voucher.get("Date", ""))
            # Period = YYYY-MM
            period = v_date[:7] if v_date and len(v_date) >= 7 else "Unknown"

            # Hämta detaljerad voucher med rader
            v_series = voucher.get("VoucherSeries", "")
            v_number = voucher.get("VoucherNumber", "")
            if v_series and v_number is not None:
                try:
                    fy_param = f"?financialyear={req.financial_year}" if req.financial_year else ""
                    detail = await _fortnox_api_get(
                        f"/vouchers/{v_series}/{v_number}{fy_param}",
                        token,
                    )
                    v_rows = detail.get("Voucher", {}).get("VoucherRows", [])
                    for row in v_rows:
                        acc_nr = row.get("Account", 0)
                        rows.append({
                            "period": period,
                            "account": str(acc_nr),
                            "account_name": accounts_map.get(acc_nr, {}).get("description", ""),
                            "actual": float(row.get("Debit", 0) or 0) - float(row.get("Credit", 0) or 0),
                            "cost_center": row.get("CostCenter", ""),
                            "project": row.get("Project", ""),
                        })
                except Exception as e:
                    print(f"[Fortnox] Voucher detail error {v_series}-{v_number}: {e}")

        meta = data.get("MetaInformation", {})
        if page >= meta.get("@TotalPages", 1):
            break
        page += 1

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

    total_actual = float(by_account["actual"].sum())

    # Period series (tidsserier)
    period_series = []
    for p in periods:
        p_sum = float(df[df["period"] == p]["actual"].sum())
        period_series.append({"period": p, "actual": round(p_sum, 0), "budget": 0})

    # MoM
    mom_diff, mom_pct = None, None
    if previous_period:
        cur_total = float(df[df["period"] == current_period]["actual"].sum())
        prev_total = float(df[df["period"] == previous_period]["actual"].sum())
        mom_diff = cur_total - prev_total
        mom_pct = (cur_total - prev_total) / abs(prev_total) if prev_total != 0 else None

    # Top konton (störst belopp)
    by_account_sorted = by_account.sort_values("actual", ascending=True)
    top_budget = []
    for _, r in by_account_sorted.head(10).iterrows():
        top_budget.append({
            "Konto": str(r["account"]),
            "Label": r["account_name"] or str(r["account"]),
            "Utfall": round(float(r["actual"]), 0),
            "Budget": 0,
            "Vs budget diff": round(float(r["actual"]), 0),
            "Vs budget %": 0,
        })

    top_mom = []
    for _, r in by_account_sorted.tail(10).iterrows():
        top_mom.append({
            "Konto": str(r["account"]),
            "Label": r["account_name"] or str(r["account"]),
            "Utfall": round(float(r["actual"]), 0),
            "Budget": 0,
            "Vs budget diff": round(float(r["actual"]), 0),
            "Vs budget %": 0,
        })

    kpi_summary = {
        "Nu": round(total_actual, 0),
        "Föregående": round(float(df[df["period"] == previous_period]["actual"].sum()), 0) if previous_period else None,
        "MoM diff": round(mom_diff, 0) if mom_diff is not None else None,
        "MoM %": round(mom_pct, 4) if mom_pct is not None else None,
        "Budget": 0,
        "Vs budget diff": round(total_actual, 0),
        "Vs budget %": 0,
    }

    pack = {
        "source": "fortnox",
        "current_period": current_period,
        "previous_period": previous_period,
        "periods": periods,
        "warnings": ["Budget-data saknas från Fortnox — lägg till manuellt eller ladda upp budgetfil."],
        "narrative": f"Period {current_period}: Utfall {total_actual:,.0f} SEK från Fortnox ({len(rows)} transaktioner, {len(accounts_map)} konton).",
        "top_budget": top_budget,
        "top_mom": top_mom,
        "kpi_summary": [kpi_summary],
        "total_actual": round(total_actual, 0),
        "total_budget": 0,
        "period_series": period_series,
        "account_rows": by_account.fillna(0).to_dict(orient="records"),
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
