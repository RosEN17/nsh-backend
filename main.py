"""
NordSheet AI — Backend API
"""

import os
import httpx
import json
import base64
from fastapi import FastAPI, HTTPException, Request
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

RESEND_API_KEY       = os.getenv("RESEND_API_KEY", "")
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")


# ─────────────────────────────────────────────────────────────────────────────
def get_user_id(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return ""
    token = auth_header.replace("Bearer ", "").strip()
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload_b64 = parts[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("sub", "")
    except Exception:
        return ""


async def get_company_id(user_id: str) -> str:
    if not user_id or not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/companies",
                params={"user_id": f"eq.{user_id}", "select": "id", "limit": "1"},
                headers={
                    "apikey":        SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                },
            )
        if r.status_code == 200:
            data = r.json()
            if data:
                return data[0]["id"]
    except Exception:
        pass
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Request-modeller
# ─────────────────────────────────────────────────────────────────────────────

class ImageData(BaseModel):
    name: str
    data: str


class EstimateRequest(BaseModel):
    description: str
    job_type: Optional[str] = None
    area_sqm: Optional[float] = None
    location: Optional[str] = None
    address: Optional[str] = None              # Fullständig adress (för tull-detektion)
    distance_km: Optional[float] = None        # Enkel väg t/r
    work_days: Optional[int] = None            # Uppskattat antal arbetsdagar
    quality: Optional[str] = "standard"        # 'standard' eller 'premium'
    hourly_rate: Optional[float] = 650
    include_rot: bool = True
    margin_pct: Optional[float] = 15
    ue_markup_pct: Optional[float] = 12.5
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
    all_edits: Optional[Dict[str, Any]] = None
    source_id: Optional[str] = None             # Spårbarhet — vilken databasrad var källan
    source_table: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "nordsheet-ai"}


@app.post("/api/estimate")
async def estimate(req: EstimateRequest, request: Request):
    user_id = get_user_id(request)
    company_id = await get_company_id(user_id)

    try:
        result = await generate_estimate(
            description=req.description,
            job_type=req.job_type,
            area_sqm=req.area_sqm,
            location=req.location,
            address=req.address,
            distance_km=req.distance_km,
            work_days=req.work_days,
            quality=req.quality or "standard",
            hourly_rate=req.hourly_rate or 650,
            include_rot=req.include_rot,
            margin_pct=req.margin_pct or 15,
            ue_markup_pct=req.ue_markup_pct or 12.5,
            build_params=req.build_params,
            images=req.images,
            documents=req.documents,
            company_id=company_id,
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
async def save_feedback(req: FeedbackRequest, request: Request):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="Supabase inte konfigurerat")
    if req.reason_code not in VALID_REASON_CODES:
        raise HTTPException(status_code=400, detail="Ogiltigt reason_code.")
    if req.reason_code == "other" and not req.reason_text:
        raise HTTPException(status_code=400, detail="reason_text krävs när reason_code är 'other'")

    user_id    = get_user_id(request)
    company_id = await get_company_id(user_id)

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
            "company_id":     company_id or None,
            "source_id":      req.source_id or None,
            "source_table":   req.source_table or None,
        }
        r1 = await http.post(
            f"{SUPABASE_URL}/rest/v1/feedback_events",
            headers=headers, json=feedback_row,
        )
        if r1.status_code >= 400:
            print(f"Varning: feedback_events sparades inte: {r1.status_code} {r1.text}")
        if req.all_edits:
            r2 = await http.patch(
                f"{SUPABASE_URL}/rest/v1/quotes",
                params={"quote_number": f"eq.{req.quote_number}"},
                headers=headers, json={"craftsman_edits": req.all_edits},
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
            <div style="font-size: 20px; font-weight: 700; color: #16a34a; margin-bottom: 4px;">Offert godkänd!</div>
            <div style="font-size: 14px; color: #64748b;">En kund har godkänt din offert</div>
        </div>
        <div style="background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; margin-bottom: 24px;">
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px;">
                <span style="color: #64748b;">Offert: </span><strong>{req.quote_title}</strong>
            </div>
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px;">
                <span style="color: #64748b;">Kund: </span><strong>{req.customer_name}</strong>
            </div>
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px;">
                <span style="color: #64748b;">E-post: </span>{req.customer_email}
            </div>
            <div style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-size: 13px;">
                <span style="color: #64748b;">Belopp: </span><strong>{req.total_amount}</strong>
            </div>
            <div style="padding: 8px 0; font-size: 13px;">
                <span style="color: #64748b;">Godkänd: </span>{req.accepted_date}
            </div>
        </div>
    </div>
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "from":    "NordSheet <noreply@nordsheet.com>",
                    "to":      [req.company_email],
                    "subject": f"Offert godkänd: {req.quote_title} - {req.customer_name}",
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


# ─────────────────────────────────────────────────────────────────────────────
# Admin: hämta hela prislådan för admin-UI
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/admin/pricing/{job_type}")
async def get_pricing(job_type: str, quality: str = "standard", region: str = "default"):
    """
    Returnerar hela prislådan för en jobbtyp.
    Används av admin-UI för att visa och redigera priser.
    """
    from app.services.ai import fetch_pricing_context
    ctx = await fetch_pricing_context(job_type=job_type, quality=quality, region=region)
    return ctx


@app.get("/api/job-types")
def job_types():
    return [
        {"id": "rivning",     "label": "Rivning",      "icon": "🔨", "enabled": True},
        {"id": "fasad",       "label": "Fasad",        "icon": "🧱", "enabled": True},
        {"id": "altan",       "label": "Altan/Trall",  "icon": "🪵", "enabled": True},
        # Kvarvarande från tidigare versioner — datamodellen stöder dem
        # men vi har inte seedat priser ännu
        {"id": "badrum",      "label": "Badrum",       "icon": "🚿", "enabled": False},
        {"id": "kok",         "label": "Kök",          "icon": "🍳", "enabled": False},
        {"id": "tak",         "label": "Tak",          "icon": "🏠", "enabled": False},
        {"id": "tillbyggnad", "label": "Tillbyggnad",  "icon": "📐", "enabled": False},
        {"id": "ovrigt",      "label": "Övrigt",       "icon": "🔨", "enabled": True},
    ]

class OutcomeRequest(BaseModel):
    quote_id: str
    outcome: str                          # 'won' | 'lost' | 'pending'
    actual_final_price: Optional[float] = None
    lost_reason: Optional[str] = None


@app.post("/api/quotes/{quote_id}/outcome")
async def update_quote_outcome(
    quote_id: str,
    req: OutcomeRequest,
    request: Request,
):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="Supabase inte konfigurerat")
    if req.outcome not in {"won", "lost", "pending"}:
        raise HTTPException(status_code=400, detail="Ogiltigt outcome-värde")

    user_id = get_user_id(request)

    update_data: dict = {"outcome": req.outcome}

    if req.actual_final_price is not None:
        update_data["actual_final_price"] = req.actual_final_price

    if req.outcome == "lost" and req.lost_reason:
        update_data["lost_reason"] = req.lost_reason

    if req.outcome == "won" and req.actual_final_price:
        # Beräkna och logga avvikelsen i feedback_events automatiskt
        headers = {
            "apikey":        SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type":  "application/json",
        }
        async with httpx.AsyncClient(timeout=10.0) as http:
            # Hämta offerten för att få ai-priset
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/quotes",
                params={"id": f"eq.{quote_id}", "select": "total_inc_vat,project_type,region,quote_number"},
                headers=headers,
            )
            if r.status_code == 200 and r.json():
                q = r.json()[0]
                ai_price = q.get("total_inc_vat", 0)
                diff_pct = round(
                    abs(req.actual_final_price - ai_price) / max(ai_price, 1) * 100, 1
                )
                # Spara som feedback_event så avvikelsen syns i kalibrerings-queryn
                await http.post(
                    f"{SUPABASE_URL}/rest/v1/feedback_events",
                    headers={**headers, "Prefer": "return=minimal"},
                    json={
                        "quote_number":  q.get("quote_number", quote_id),
                        "field_changed": "total_final_price",
                        "ai_value":      str(round(ai_price)),
                        "final_value":   str(round(req.actual_final_price)),
                        "reason_code":   "market_price" if diff_pct > 10 else "other",
                        "reason_text":   f"Faktiskt slutpris: {req.actual_final_price} kr (avvikelse {diff_pct}%)",
                        "job_type":      q.get("project_type", ""),
                        "region":        q.get("region", ""),
                    },
                )

    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.patch(
            f"{SUPABASE_URL}/rest/v1/quotes",
            params={"id": f"eq.{quote_id}"},
            headers={
                "apikey":        SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "return=minimal",
            },
            json=update_data,
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=500, detail=f"Kunde inte uppdatera offert: {r.text}")

    return {"success": True, "outcome": req.outcome}
