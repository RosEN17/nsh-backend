"""
ByggKalk AI — Backend API
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from app.services.ai import generate_estimate, chat_about_estimate

app = FastAPI(title="ByggKalk AI API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EstimateRequest(BaseModel):
    description: str
    job_type: Optional[str] = None
    area_sqm: Optional[float] = None
    location: Optional[str] = None
    hourly_rate: Optional[float] = 650
    include_rot: bool = True
    margin_pct: Optional[float] = 15


class ChatRequest(BaseModel):
    message: str
    estimate_context: Optional[dict] = None


@app.get("/health")
def health():
    return {"status": "ok", "service": "byggkalk-ai"}


@app.post("/api/estimate")
async def create_estimate_endpoint(req: EstimateRequest):
    try:
        result = await generate_estimate(
            description=req.description,
            job_type=req.job_type,
            area_sqm=req.area_sqm,
            location=req.location,
            hourly_rate=req.hourly_rate or 650,
            include_rot=req.include_rot,
            margin_pct=req.margin_pct or 15,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat_endpoint(req: ChatRequest):
    try:
        reply = await chat_about_estimate(req.message, req.estimate_context)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/job-types")
def job_types():
    return {
        "job_types": [
            {"id": "badrum", "label": "Badrumsrenovering", "icon": "🚿"},
            {"id": "kok", "label": "Köksrenovering", "icon": "🍳"},
            {"id": "tak", "label": "Takbyte", "icon": "🏠"},
            {"id": "fasad", "label": "Fasadrenovering", "icon": "🧱"},
            {"id": "golv", "label": "Golvläggning", "icon": "🪵"},
            {"id": "malning", "label": "Målning", "icon": "🎨"},
            {"id": "el", "label": "Elinstallation", "icon": "⚡"},
            {"id": "vvs", "label": "VVS-arbete", "icon": "🔧"},
            {"id": "tillbyggnad", "label": "Tillbyggnad", "icon": "🏗️"},
            {"id": "ovrigt", "label": "Övrigt", "icon": "📋"},
        ]
    }
