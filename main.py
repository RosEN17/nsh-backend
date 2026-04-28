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
    company_id: Optional[str] = None


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


VALID_REASON_CODES = {
    "difficult_access", "hidden_damage", "customer_request",
    "wrong_material", "market_price", "scope_change", "wrong_hours", "other",
}


class FeedbackRequest(BaseModel):
    quote_number: str
    field_changed: str
    ai_value: str
    final_value: str
    reason_code: str
    reason_text: Optional[str] = None
    craftsman_name: Optional[str] = None
    job_type: Optional[str] = None
    region: Optional[str] = None
    company_id: Optional[str] = None
    all_edits: Optional[Dict[str, Any]] = None


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
            company_id=req.company_id or "",
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


@app.post("/api/feedback")
async def save_feedback(req: FeedbackRequest):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="Supabase inte konfigurerat")

    if req.reason_code not in VALID_REASON_CODES:
        raise HTTPException(status_code=400, detail=f"Ogiltigt reason_code.")

    if req.reason_code == "other" and not req.reason_text:
        raise HTTPException(status_code=400, detail="reason_text kravs nar reason_code ar 'other'")

    headers = {
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

    async with httpx.AsyncClient(timeout=10.0) as http:
        feedback_row = {
            "quote_number":   req.quote_number,
            "field_changed":  req.field_changed,
            "ai_value":       req.ai_value,
            "final_value":    req.final_value,
            "reason_code":    req.reason_code,
            "reason_text":    req.reason_text or "",
            "craftsman_name": req.craftsman_name or "",
            "job_type":       req.job_type or "",
            "region":         req.region or "",
            "company_id":     req.company_id or None,
        }

        r1 = await http.post(
            f"{SUPABASE_URL}/rest/v1/feedback_events",
            headers=headers,
            json=feedback_row,
        )
        if r1.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"Kunde inte spara: {r1.text}")

        if req.all_edits:
            r2 = await http.patch(
                f"{SUPABASE_URL}/rest/v1/quotes",
                params={"quote_number": f"eq.{req.quote_number}"},
                headers=headers,
                json={"craftsman_edits": req.all_edits},
            )
            if r2.status_code >= 400:
                print(f"Varning: craftsman_edits uppdaterades inte: {r2.text}")

    return {"success": True, "message": "Feedback sparad"}


@app.post("/api/notify-acceptance")
async def notify_acceptance(req: AcceptNotifyRequest):
    if not RESEND_API_KEY:
        raise HTTPException(status_code=500, detail="RESEND_API_KEY not configured")

    html_body = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 32px;">
        <div style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 12px; padding: 24px; margin-bottom: 24px; text-align: center;">
            <div style="font-size: 20px; font-weight: 700; color: #16a34a;">Offert godkand!</div>
            <div style="font-size: 14px; color: #64748b;">En kund har godkant din offert</div>
        </div>
        <div style="background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; margin-bottom: 24px;">
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9;">Offert: <strong>{req.quote_title}</strong></div>
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9;">Kund: <strong>{req.customer_name}</strong></div>
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9;">E-post: {req.customer_email}</div>
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9;">Belopp: <strong>{req.total_amount}</strong></div>
            <div style="padding: 8px 0;">Godkand: {req.accepted_date}</div>
        </div>
    </div>
    """

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from":    "NordSheet <noreply@nordsheet.com>",
                    "to":      [req.company_email],
                    "subject": f"Offert godkand: {req.quote_title} - {req.customer_name}",
                    "html":    html_body,
                },
            )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=f"Resend error: {response.text}")
        return {"success": True, "message": "Notifikation skickad"}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Mail error: {str(e)}")


@app.get("/api/norms/{job_type}")
async def get_norms(job_type: str):
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
