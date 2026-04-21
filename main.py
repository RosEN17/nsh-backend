"""
NordSheet AI — Backend API
"""

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


class ImageData(BaseModel):
    name: str
    data: str  # base64 data URL


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


@app.get("/api/job-types")
def job_types():
    return [
        {"id": "badrum", "label": "Badrum", "icon": "🚿"},
        {"id": "kok", "label": "Kök", "icon": "🍳"},
        {"id": "golv", "label": "Golv", "icon": "🪵"},
        {"id": "malning", "label": "Målning", "icon": "🎨"},
        {"id": "tak", "label": "Tak", "icon": "🏠"},
        {"id": "el", "label": "El", "icon": "⚡"},
        {"id": "vvs", "label": "VVS", "icon": "🔧"},
        {"id": "fasad", "label": "Fasad", "icon": "🧱"},
        {"id": "tillbyggnad", "label": "Tillbyggnad", "icon": "📐"},
        {"id": "ovrigt", "label": "Övrigt", "icon": "🔨"},
    ]
