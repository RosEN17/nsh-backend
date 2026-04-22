"""
NordSheet AI — Backend API
"""

import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List
from app.services.ai import generate_estimate, chat_about_estimate

app = FastAPI(title="NordSheet AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")


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
                    "from": "NordSheet <onboarding@resend.dev>",
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


@app.get("/api/job-types")
def job_types():
    return [
        {"id": "badrum", "label": "Badrum", "icon": "🚿"},
        {"id": "kok", "label": "Kok", "icon": "🍳"},
        {"id": "golv", "label": "Golv", "icon": "🪵"},
        {"id": "malning", "label": "Malning", "icon": "🎨"},
        {"id": "tak", "label": "Tak", "icon": "🏠"},
        {"id": "el", "label": "El", "icon": "⚡"},
        {"id": "vvs", "label": "VVS", "icon": "🔧"},
        {"id": "fasad", "label": "Fasad", "icon": "🧱"},
        {"id": "tillbyggnad", "label": "Tillbyggnad", "icon": "📐"},
        {"id": "ovrigt", "label": "Ovrigt", "icon": "🔨"},
    ]
