from pydantic import BaseModel, Field
import config
from typing import Optional
from enum import Enum


class IncidentStatus(str, Enum):
    NEW = "new"
    ALERTED = "alerted"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"
    CLOSED = "closed"


class SeverityLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class LocationSource(str, Enum):
    EXIF = "exif"
    BROWSER = "browser"
    MANUAL = "manual"
    WHATSAPP = "whatsapp"
    WHATSAPP_DEMO = "whatsapp_demo"
    UNKNOWN = "unknown"


# --- Request Models ---

class ChatQueryRequest(BaseModel):
    message: str = Field(min_length=1, max_length=config.MAX_CHAT_MESSAGE_CHARS)
    session_id: Optional[str] = None
    lat: Optional[float] = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    lng: Optional[float] = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    location_source: Optional[LocationSource] = None


class LocationUpdateRequest(BaseModel):
    incident_id: str
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    source: LocationSource = LocationSource.MANUAL


class AdminQueryRequest(BaseModel):
    query: str
    admin_password: str


class IncidentStatusUpdate(BaseModel):
    status: IncidentStatus
    admin_password: str


# --- Response Models ---

class TriageResult(BaseModel):
    severity: SeverityLevel
    severity_score: int = Field(ge=1, le=10)
    confidence: float = Field(ge=0.0, le=1.0)
    indicators: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    escalation_needed: bool = False
    triage_summary: str = ""


class SimilarityResult(BaseModel):
    is_exact_duplicate: bool = False
    exact_match_id: Optional[str] = None
    similar_incidents: list[dict] = Field(default_factory=list)
    message: str = ""


class ResourceLink(BaseModel):
    label: str
    url: str
    phone: str = ""
    address: str = ""
    opening_hours: str = ""


class ChatResponse(BaseModel):
    response: str
    answer_format: str = "text"
    incident_id: Optional[str] = None
    triage: Optional[TriageResult] = None
    similarity: Optional[SimilarityResult] = None
    escalation_triggered: bool = False
    escalation_status: Optional[str] = None
    # None = location unknown; True = verified in region; False = outside jurisdiction
    in_jurisdiction: Optional[bool] = None
    # Only returned when STRICT_LOCATION_GATE=false and API is waiting for user confirmation.
    location_confirmed_needed: Optional[bool] = None
    # Pass to /v1/triage/confirm when location_confirmed_needed=True.
    pending_token: Optional[str] = None
    resource_links: list[ResourceLink] = Field(default_factory=list)
    # Audit details explaining which verified coordinate the jurisdiction gate used.
    location_verification: Optional[dict] = None


class AdminQueryResponse(BaseModel):
    query: str
    sql_generated: str
    results: list[dict] = Field(default_factory=list)
    row_count: int = 0
    summary: str = ""


class IncidentDetail(BaseModel):
    incident_id: str
    created_at: str
    status: str
    triage_severity: Optional[str] = None
    triage_confidence: Optional[float] = None
    triage_summary: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    location_source: Optional[str] = None
    similar_incident_id: Optional[str] = None
    similarity_score: Optional[float] = None
    image_url: Optional[str] = None


class AlertPayload(BaseModel):
    incident_id: str
    timestamp: str
    severity: str
    severity_score: int
    confidence: float
    distress_indicators: list[str] = Field(default_factory=list)
    location: Optional[dict] = None
    similar_incident_reference: Optional[str] = None
    admin_console_url: str = ""
