from typing import Optional, List, Dict, Any
from pydantic import BaseModel, EmailStr

class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str
    title: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class MappingRequest(BaseModel):
    period: str
    account: str
    actual: str
    budget: Optional[str] = None
    entity: Optional[str] = None
    cost_center: Optional[str] = None
    project: Optional[str] = None
    account_name: Optional[str] = None

class AnalyzeResponse(BaseModel):
    current_period: str
    previous_period: Optional[str]
    kpi_summary: List[Dict[str, Any]]
    top_mom: List[Dict[str, Any]]
    top_budget: List[Dict[str, Any]]
    drivers_account_mom: List[Dict[str, Any]]
    drivers_account_budget: List[Dict[str, Any]]
    narrative: str
    data_model_ok: bool
    warnings: List[str]
    available_columns: List[str]
    column_suggestions: Dict[str, List[str]]

class ChatRequest(BaseModel):
    question: str
    pack: Dict[str, Any]

class ReportDraftRequest(BaseModel):
    pack: Dict[str, Any]
    report_type: str
    audience: str
    tone: str
    sections: List[str]
    report_items: List[Dict[str, Any]]

class ExportRequest(BaseModel):
    fmt: str
    pack: Dict[str, Any]
    report_items: List[Dict[str, Any]]
    spec: Dict[str, Any]
    business_type: Optional[str] = "Övrigt"
    purpose: Optional[str] = "report"