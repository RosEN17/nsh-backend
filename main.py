import io
import json
import pandas as pd
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

def read_upload_bytes(b: bytes, filename: str):
    import os, io
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".csv":
        df = pd.read_csv(io.BytesIO(b))
    elif ext in {".xlsx", ".xls"}:
        df = pd.read_excel(io.BytesIO(b))
    else:
        raise ValueError("Endast CSV, XLSX och XLS stöds.")
    return df, b

from app.schemas import (
    SignupRequest,
    LoginRequest,
    ChatRequest,
    ReportDraftRequest,
    ExportRequest,
)
from app.services.analysis import (
    Mapping,
    VariancePack,
    read_upload,
    infer_candidate_columns,
    choose_auto_mapping,
    build_model_df,
    compute_variances,
    build_issues,
    pack_to_dict,
    dict_to_pack,
)

from app.services.auth import create_user_account, authenticate_user
from app.services.ai import ask_ai, generate_ai_report_draft
from app.services.export import export_artifact
from app.services.analysis import (
    Mapping,
    read_upload,
    infer_candidate_columns,
    build_model_df,
    compute_variances,
    VariancePack,
)

app = FastAPI(title="NordSheet API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*nordsheet\.com|https://nsh-frontend.*\.vercel\.app|http://localhost:3000",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def pack_to_dict(pack):
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

def dict_to_pack(data: dict):
    return VariancePack(
        current_period=data["current_period"],
        previous_period=data.get("previous_period"),
        kpi_summary=pd.DataFrame(data.get("kpi_summary", [])),
        top_mom=pd.DataFrame(data.get("top_mom", [])),
        top_budget=pd.DataFrame(data.get("top_budget", [])),
        drivers_account_mom=pd.DataFrame(data.get("drivers_account_mom", [])),
        drivers_account_budget=pd.DataFrame(data.get("drivers_account_budget", [])),
        narrative=data.get("narrative", ""),
        data_model_ok=data.get("data_model_ok", False),
        warnings=data.get("warnings", []),
        model_df=pd.DataFrame(),
    )

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/signup")
def signup(payload: SignupRequest):
    ok, msg = create_user_account(payload.name, payload.email, payload.password, payload.title)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"ok": True, "message": msg}

@app.post("/api/login")
def login(payload: LoginRequest):
    ok, user, msg = authenticate_user(payload.email, payload.password)
    if not ok:
        raise HTTPException(status_code=401, detail=msg)
    return {"ok": True, "user": user, "message": msg}

@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        df, _ = read_upload_bytes(contents, file.filename)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    suggestions = infer_candidate_columns(df)
    return {
        "available_columns": list(df.columns),
        "column_suggestions": suggestions,
        "preview": df.head(20).fillna("").to_dict(orient="records"),
    }

@app.post("/api/analyze-with-mapping")
async def analyze_with_mapping(
    file: UploadFile = File(...),
    mapping_json: str = Form(...),
):
    contents = await file.read()
    df, _ = read_upload_bytes(contents, file.filename)
    mapping_data = json.loads(mapping_json)
    mapping = Mapping(**mapping_data)
    model_df = build_model_df(df, mapping)
    pack = compute_variances(model_df)
    return {
        **pack_to_dict(pack),
        "available_columns": list(df.columns),
        "column_suggestions": infer_candidate_columns(df),
    }

@app.post("/api/chat")
def chat(payload: ChatRequest):
    pack = dict_to_pack(payload.pack)
    answer = ask_ai(payload.question, pack)
    return {"answer": answer}

@app.post("/api/report-draft")
def report_draft(payload: ReportDraftRequest):
    pack = dict_to_pack(payload.pack)
    draft = generate_ai_report_draft(
        pack=pack,
        report_type=payload.report_type,
        audience=payload.audience,
        tone=payload.tone,
        sections=payload.sections,
        report_items=payload.report_items,
    )
    return {"draft": draft}

@app.post("/api/export")
def export_file(payload: ExportRequest):
    pack = dict_to_pack(payload.pack)
    data, media_type, filename = export_artifact(
        fmt=payload.fmt,
        pack=pack,
        report_items=payload.report_items,
        spec=payload.spec,
        purpose=payload.purpose,
        btype=payload.business_type,
    )

    return StreamingResponse(
        io.BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
