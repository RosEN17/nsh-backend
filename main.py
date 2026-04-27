"""
NordSheet AI — Backend API
"""

import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List, Any
from app.services.ai import generate_estimate, chat_about_estimate

app = FastAPI(title="NordSheet AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RESEND_API_KEY        = os.getenv("RESEND_API_KEY", "")
SUPABASE_URL          = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY  = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


class ImageData(BaseModel):
    name: str
    data: str


class EstimateRequest(BaseModel):
    description: str
    job_type: Optional[str] = None
    area_sqm: Optional[float] = None
    location: Optional[str] = None
    hourly_rate: Optional[float] = 650
    include_rot: bool = True
    margin_pct: Optional[float] = 15
    build_params: Optional[Dict[str, str]] = None
    images: Optional[List[ImageData]] = None
    documents: Optional[List[ImageData]] = None


class ChatRequest(BaseModel):
    message: str
    estimate_context: Optional[dict] = None


class AcceptNotifyRequest(BaseModel):
    company_email: str
    company_name: str
    quote_title: str
    customer_name: str
    customer_email: str
    total_amount: str
    accepted_date: str


# --- NY: Feedback från snickare när de redigerar AI-offert ---
class FeedbackRequest(BaseModel):
    quote_number: str                  # ex. "D1849-3"
    field_changed: str                 # ex. "labor_cost"
    ai_value: str                      # vad AI föreslog, alltid som sträng
    final_value: str                   # vad snickaren satte
    reason_code: str                   # se reason_codes nedan
    reason_text: Optional[str] = None  # fritext, krävs om reason_code = "other"
    craftsman_name: Optional[str] = None
    job_type: Optional[str] = None
    region: Optional[str] = None
    # Hela edits-objektet för att uppdatera quotes.craftsman_edits
    all_edits: Optional[Dict[str, Any]] = None


# Giltiga reason_codes — valideras i endpointen
VALID_REASON_CODES = {
    "difficult_access",   # Svår åtkomst
    "hidden_damage",      # Dolda skador/fukt
    "customer_request",   # Kundönskemål
    "wrong_material",     # Fel material valt av AI
    "market_price",       # Marknadspriset stämmer inte
    "scope_change",       # Scopet var bredare än beskrivet
    "other",              # Annat — kräver reason_text
}


@app.get("/health")
def health():
    return {"status": "ok", "service": "nordsheet-ai"}


@app.post("/api/estimate")
async def estimate(req: EstimateRequest):
    try:
        result = await generate_estimate(
            description=req.description,
            job_type=req.job_type,
            area_sqm=req.area_sqm,
            location=req.location,
            hourly_rate=req.hourly_rate or 650,
            include_rot=req.include_rot,
            margin_pct=req.margin_pct or 15,
            build_params=req.build_params,
            images=req.images,
            documents=req.documents,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    try:
        reply = await chat_about_estimate(req.message, req.estimate_context)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- NY ENDPOINT: Spara snickares justering ---
@app.post("/api/feedback")
async def save_feedback(req: FeedbackRequest):
    """
    Anropas varje gång en snickare ändrar ett värde i AI-offerten.
    Gör två saker samtidigt:
      1. Loggar en rad i feedback_events (för månadsanalys)
      2. Uppdaterar quotes.craftsman_edits med hela edits-objektet
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="Supabase inte konfigurerat")

    # Validera reason_code
    if req.reason_code not in VALID_REASON_CODES:
        raise HTTPException(
            status_code=400,
            detail=f"Ogiltigt reason_code. Välj ett av: {', '.join(VALID_REASON_CODES)}"
        )

    # "other" kräver fritext
    if req.reason_code == "other" and not req.reason_text:
        raise HTTPException(
            status_code=400,
            detail="reason_text krävs när reason_code är 'other'"
        )

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    async with httpx.AsyncClient(timeout=10.0) as http:

        # 1. Logga i feedback_events
        feedback_row = {
            "quote_number":  req.quote_number,
            "field_changed": req.field_changed,
            "ai_value":      req.ai_value,
            "final_value":   req.final_value,
            "reason_code":   req.reason_code,
            "reason_text":   req.reason_text or "",
            "craftsman_name": req.craftsman_name or "",
            "job_type":      req.job_type or "",
            "region":        req.region or "",
        }

        r1 = await http.post(
            f"{SUPABASE_URL}/rest/v1/feedback_events",
            headers=headers,
            json=feedback_row,
        )

        if r1.status_code >= 400:
            raise HTTPException(
                status_code=500,
                detail=f"Kunde inte spara feedback_event: {r1.text}"
            )

        # 2. Uppdatera quotes.craftsman_edits om all_edits skickades med
        if req.all_edits:
            r2 = await http.patch(
                f"{SUPABASE_URL}/rest/v1/quotes",
                params={"quote_number": f"eq.{req.quote_number}"},
                headers=headers,
                json={"craftsman_edits": req.all_edits},
            )

            if r2.status_code >= 400:
                # Logga men kasta inte fel — feedback_event är redan sparad
                print(f"Varning: craftsman_edits uppdaterades inte: {r2.text}")

    return {"success": True, "message": "Feedback sparad"}


@app.post("/api/notify-acceptance")
async def notify_acceptance(req: AcceptNotifyRequest):
    """Skickar ett mail till foretaget nar en offert godkanns."""
    if not RESEND_API_KEY:
        raise HTTPException(status_code=500, detail="RESEND_API_KEY not configured")

    html_body = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 32px;">
        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 24px; margin-bottom: 24px; text-align: center;">
            <div style="font-size: 40px; margin-bottom: 8px;">✅</div>
            <div style="font-size: 20px; font-weight: 700; color: #16a34a; margin-bottom: 4px;">Offert godkand!</div>
            <div style="font-size: 14px; color: #64748b;">En kund har godkant din offert</div>
        </div>
        <div style="background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; margin-bottom: 24px;">
            <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1.5px; color: #94a3b8; margin-bottom: 12px;">Offertdetaljer</div>
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f1f5f9;">
                <span style="font-size: 13px; color: #64748b;">Offert</span>
                <span style="font-size: 13px; font-weight: 600; color: #0f172a;">{req.quote_title}</span>
            </div>
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f1f5f9;">
                <span style="font-size: 13px; color: #64748b;">Kund</span>
                <span style="font-size: 13px; font-weight: 600; color: #0f172a;">{req.customer_name}</span>
            </div>
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f1f5f9;">
                <span style="font-size: 13px; color: #64748b;">E-post</span>
                <span style="font-size: 13px; color: #0f172a;">{req.customer_email}</span>
            </div>
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #f1f5f9;">
                <span style="font-size: 13px; color: #64748b;">Belopp</span>
                <span style="font-size: 13px; font-weight: 700; color: #0f172a;">{req.total_amount}</span>
            </div>
            <div style="display: flex; justify-content: space-between; padding: 8px 0;">
                <span style="font-size: 13px; color: #64748b;">Godkand</span>
                <span style="font-size: 13px; color: #0f172a;">{req.accepted_date}</span>
            </div>
        </div>
        <div style="background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 12px; padding: 20px; text-align: center;">
            <div style="font-size: 14px; font-weight: 600; color: #1e40af; margin-bottom: 8px;">Nasta steg</div>
            <div style="font-size: 13px; color: #3b82f6;">Skapa projektet i Bygglet och kontakta kunden for att boka in start.</div>
        </div>
        <div style="text-align: center; margin-top: 24px; font-size: 11px; color: #94a3b8;">
            Skickat fran NordSheet · nordsheet.com
        </div>
    </div>
    """

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": "NordSheet <noreply@nordsheet.com>",
                    "to": [req.company_email],
                    "subject": f"Offert godkand: {req.quote_title} — {req.customer_name}",
                    "html": html_body,
                },
            )

        if response.status_code >= 400:
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Resend error: {response.text}",
            )

        return {"success": True, "message": "Notifikation skickad"}

    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Mail error: {str(e)}")


@app.get("/api/norms/{job_type}")
async def get_norms(job_type: str):
    """Hämta arbetstidsnormer för en jobbtyp — används för testning."""
    from app.services.ai import fetch_norms
    norms_text = await fetch_norms(job_type)
    return {"job_type": job_type, "norms": norms_text or "Inga normer hittades"}


@app.get("/api/job-types")
def job_types():
    return [
        {"id": "badrum",      "label": "Badrum",      "icon": "🚿"},
        {"id": "kok",         "label": "Kok",          "icon": "🍳"},
        {"id": "golv",        "label": "Golv",         "icon": "🪵"},
        {"id": "malning",     "label": "Malning",      "icon": "🎨"},
        {"id": "tak",         "label": "Tak",          "icon": "🏠"},
        {"id": "el",          "label": "El",           "icon": "⚡"},
        {"id": "vvs",         "label": "VVS",          "icon": "🔧"},
        {"id": "fasad",       "label": "Fasad",        "icon": "🧱"},
        {"id": "tillbyggnad", "label": "Tillbyggnad",  "icon": "📐"},
        {"id": "ovrigt",      "label": "Ovrigt",       "icon": "🔨"},
    ]
