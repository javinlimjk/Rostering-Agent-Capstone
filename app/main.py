# app/main.py
from fastapi import FastAPI, HTTPException, Security, status, Depends, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import date
import os
import mlflow

import logging

# Import internal modules
from app.models import Staff, Shift, RosterAssignment

logger = logging.getLogger(__name__)
from app.optimizer import RosterOptimizer
from app.forecaster import StaffingForecaster
from app.compliance import ComplianceEngine
from app.rules import get_rules_for_country
from app.agent import SchedulingAgent
from app.demand_planner import FlightService, calculate_required_staff
from celery.result import AsyncResult
from app.celery_app import celery_app
from app.tasks import (
    task_forecast,
    task_optimize,
    task_validate,
    task_recommend,
    task_agent_chat
)

from fastapi.middleware.cors import CORSMiddleware

# --- DATABASE SETUP ---
from app.database import Base, engine as db_engine, get_db
from app import models_db
from sqlalchemy.orm import Session

app = FastAPI(title="SATS Rostering API", version="6.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this in production, e.g., ["http://localhost:3000"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- SECURITY ---
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

async def verify_api_key(api_key: str = Security(api_key_header)):
    return True

# --- GLOBAL STATE ---
@app.on_event("startup")
def load_models():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    laws_path = os.path.join(base_dir, "data", "laws")
    
    logger.info(f"🚀 Server Starting... Scanning for laws in {laws_path}")
    engine = ComplianceEngine()
    
    if os.path.exists(laws_path):
        engine.load_laws(laws_path)
    else:
        logger.warning("⚠️ Warning: Laws directory not found. RAG features will be disabled.")

    app.state.compliance_engine = engine

    # Create DB tables if they don't exist
    Base.metadata.create_all(bind=db_engine)
    logger.info("✅ SQLite database tables initialized.")



    # Setup MLflow
    mlflow_dir = os.path.join(base_dir, "mlruns")
    if not os.path.exists(mlflow_dir):
        os.makedirs(mlflow_dir)
    mlflow.set_tracking_uri(f"file:{mlflow_dir}")
    mlflow.set_experiment("SATS_Roster_Optimization")
    logger.info("✅ System Ready.")

def get_compliance_engine(request: Request):
    return getattr(request.app.state, "compliance_engine", None)

# --- REQUEST SCHEMAS ---
class ForecastRequest(BaseModel):
    shift_inputs: List[Dict[str, Any]]
    days: int = 7
    country: str = "SG"
    buffer: float = 0.15
    demand_signals: Optional[Dict[str, Any]] = None

class OptimizeRequest(BaseModel):
    staff: List[Staff]
    shifts: List[Shift]
    country: str 
    rules: Optional[Dict[str, Any]] = None # Allow frontend to override rules
    demand_signal: Optional[Dict[str, int]] = None # Time-series demand { "08:00": 15 }
    target_staff_count: Optional[int] = None

class ValidateRequest(BaseModel):
    assignments: List[Dict[str, Any]]
    shift_definitions: List[Dict[str, Any]]
    country: str
    rules: Optional[Dict[str, Any]] = None
    staff: Optional[List[Staff]] = None

class RecommendationRequest(BaseModel):
    date_target: Optional[date] = None
    shift_name: Optional[str] = None
    assignments: List[Dict[str, Any]]
    shift_definitions: List[Dict[str, Any]]
    staff_list: List[Staff]
    country: str 
    violation_type: Optional[str] = None
    violator: Optional[str] = None

class MetricsRequest(BaseModel):
    assignments: List[Dict[str, Any]]

class AgentChatRequest(BaseModel):
    message: str
    state: Optional[Dict[str, Any]] = None

class SimulateDemandRequest(BaseModel):
    flightVolumeSurge: float
    weatherDelayProb: float
    holidayLoad: float
    target_staff_count: int = 50

class OracleRequest(BaseModel):
    location: str
    month_year: str

# --- ENDPOINTS ---

@app.get("/")
def health_check():
    return {"status": "active", "version": "6.0"}

@app.get("/tasks/{task_id}", dependencies=[Depends(verify_api_key)])
def get_task_status(task_id: str):
    task_result = AsyncResult(task_id, app=celery_app)
    if task_result.state == 'PENDING':
        return {"state": task_result.state, "status": "Pending..."}
    elif task_result.state != 'FAILURE':
        return {
            "state": task_result.state,
            "result": task_result.result,
        }
    else:
        # something went wrong in the background job
        return {
            "state": task_result.state,
            "error": str(task_result.info), # this is the exception raised
        }

@app.post("/forecast", dependencies=[Depends(verify_api_key)])
def get_staffing_forecast(payload: ForecastRequest):
    task = task_forecast.delay(
        payload.shift_inputs, 
        payload.days, 
        payload.country,
        payload.buffer,
        payload.demand_signals
    )
    return {"task_id": task.id}

@app.post("/optimize", dependencies=[Depends(verify_api_key)])
def generate_roster(payload: OptimizeRequest):
    staff_dicts = [s.dict() for s in payload.staff]
    shift_dicts = [s.dict() for s in payload.shifts]
    task = task_optimize.delay(
        staff_dicts,
        shift_dicts,
        payload.country,
        payload.rules,
        payload.demand_signal,
        payload.target_staff_count
    )
    return {"task_id": task.id}

@app.post("/validate", dependencies=[Depends(verify_api_key)])
def validate_roster(payload: ValidateRequest):
    staff_dicts = [s.dict() for s in payload.staff] if payload.staff else []
    task = task_validate.delay(
        payload.assignments,
        payload.shift_definitions,
        payload.country,
        payload.rules,
        staff_dicts
    )
    return {"task_id": task.id}

@app.post("/recommend", dependencies=[Depends(verify_api_key)])
def recommend_staff(payload: RecommendationRequest):
    staff_dicts = [s.dict() for s in payload.staff_list]
    date_val = payload.date_target.isoformat() if payload.date_target and isinstance(payload.date_target, date) else payload.date_target
    task = task_recommend.delay(
        date_val,
        payload.shift_name, 
        payload.assignments, 
        payload.shift_definitions,
        staff_dicts,
        payload.country,
        payload.violation_type,
        payload.violator
    )
    return {"task_id": task.id}

@app.post("/metrics", dependencies=[Depends(verify_api_key)])
def calculate_live_metrics(payload: MetricsRequest):
    # Convert simple dicts back to internal structure for calculation
    # We create dummy RosterAssignment objects
    assignments = []
    for x in payload.assignments:
        try:
            d = date.fromisoformat(x['date'])
            assignments.append(RosterAssignment(
                staff_id=x['staff_id'], 
                shift_id="manual", 
                date=d, 
                shift_type=x['shift']
            ))
        except: pass
    
    opt = RosterOptimizer([], [])
    return opt.calculate_metrics_only(assignments)

@app.get("/compliance/search", dependencies=[Depends(verify_api_key)])
def search_laws(query: str, country: str = "SG", engine: Optional[Any] = Depends(get_compliance_engine)): # Added country param
    with mlflow.start_run():
        mlflow.log_param("endpoint", "search")
        mlflow.log_param("query", query)
        mlflow.log_param("country", country)

        if not engine:
            raise HTTPException(status_code=503, detail="AI Engine not ready")

        # Pass the country code to the engine
        results = engine.check_compliance(query, country_code=country)

        mlflow.log_metric("results_count", len(results))
        return results

@app.post("/agent/chat", dependencies=[Depends(verify_api_key)])
def agent_chat(payload: AgentChatRequest):
    task = task_agent_chat.delay(payload.message, payload.state)
    return {"task_id": task.id}

@app.get("/demand/{airport_code}", dependencies=[Depends(verify_api_key)])
def get_demand(airport_code: str):
    service = FlightService()
    flights = service.get_flights(airport_code)
    demand = calculate_required_staff(flights)
    return {"demand": demand}

@app.post("/simulate_demand", dependencies=[Depends(verify_api_key)])
def simulate_demand(payload: SimulateDemandRequest):
    forecaster = StaffingForecaster()
    signals = {
        "flightVolumeSurge": payload.flightVolumeSurge,
        "weatherDelayProb": payload.weatherDelayProb,
        "holidayLoad": payload.holidayLoad
    }
    shifts, logs = forecaster.generate_demand_shifts(signals, payload.target_staff_count)
    return {"shifts": shifts, "logs": logs}

@app.post("/oracle/insights", dependencies=[Depends(verify_api_key)])
def fetch_oracle_insights(payload: OracleRequest):
    agent = SchedulingAgent()
    insights = agent.analyze_demand_events(payload.location, payload.month_year)
    return {"insights": insights}

# ── Persistent Staff DB ──────────────────────────────────────────

class StaffPayload(BaseModel):
    id: str
    name: str
    role: str = "Field Agent"
    status: str = "Active"
    contract_type: str = "Full Time"
    country: str = "Singapore"
    max_hours: int = 44
    cost_per_hour: float = 25.0
    skills: List[str] = []

@app.get("/db/staff", dependencies=[Depends(verify_api_key)])
def list_staff(db: Session = Depends(get_db)):
    rows = db.query(models_db.StaffDB).all()
    return [
        {
            "id": r.id, "name": r.name, "role": r.role,
            "status": r.status, "contract_type": r.contract_type,
            "country": r.country, "max_hours": r.max_hours,
            "cost_per_hour": r.cost_per_hour,
            "skills": r.skills.split(",") if r.skills else []
        }
        for r in rows
    ]

@app.post("/db/staff", dependencies=[Depends(verify_api_key)])
def upsert_staff(payload: StaffPayload, db: Session = Depends(get_db)):
    existing = db.query(models_db.StaffDB).filter(models_db.StaffDB.id == payload.id).first()
    if existing:
        existing.name = payload.name
        existing.role = payload.role
        existing.status = payload.status
        existing.contract_type = payload.contract_type
        existing.country = payload.country
        existing.max_hours = payload.max_hours
        existing.cost_per_hour = payload.cost_per_hour
        existing.skills = ",".join(payload.skills)
    else:
        new_staff = models_db.StaffDB(
            id=payload.id, name=payload.name, role=payload.role,
            status=payload.status, contract_type=payload.contract_type,
            country=payload.country, max_hours=payload.max_hours,
            cost_per_hour=payload.cost_per_hour,
            skills=",".join(payload.skills)
        )
        db.add(new_staff)
    db.commit()
    return {"status": "ok", "id": payload.id}

@app.delete("/db/staff/{staff_id}", dependencies=[Depends(verify_api_key)])
def delete_staff(staff_id: str, db: Session = Depends(get_db)):
    db.query(models_db.StaffDB).filter(models_db.StaffDB.id == staff_id).delete()
    db.commit()
    return {"status": "deleted"}

# ── Persistent Roster Assignments DB ────────────────────────────

class RosterAssignmentPayload(BaseModel):
    assignments: List[Dict[str, Any]]

@app.post("/db/roster/save", dependencies=[Depends(verify_api_key)])
def save_roster(payload: RosterAssignmentPayload, db: Session = Depends(get_db)):
    """Saves an entire roster snapshot with a shared batch timestamp."""
    import time
    batch_timestamp = time.time()
    
    if not payload.assignments:
        return {"status": "ok", "saved": 0}
    
    # Collect all unique dates in this save
    dates_in_payload = {a["date"] for a in payload.assignments}
    # Delete old assignments for these specific dates (clean slate per date range)
    for d in dates_in_payload:
        db.query(models_db.RosterAssignmentDB).filter(models_db.RosterAssignmentDB.date == d).delete()
    
    for assignment in payload.assignments:
        row = models_db.RosterAssignmentDB(
            staff_id=assignment["staff_id"],
            date=assignment["date"],
            shift_type=assignment["shift_type"],
            created_at=batch_timestamp
        )
        db.add(row)
    db.commit()
    return {"status": "ok", "saved": len(payload.assignments), "batch_id": batch_timestamp}


@app.get("/db/roster", dependencies=[Depends(verify_api_key)])
def load_roster(start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(models_db.RosterAssignmentDB)
    if start_date:
        query = query.filter(models_db.RosterAssignmentDB.date >= start_date)
    if end_date:
        query = query.filter(models_db.RosterAssignmentDB.date <= end_date)
    rows = query.all()
    return [
        {"staff_id": r.staff_id, "date": r.date, "shift_type": r.shift_type}
        for r in rows
    ]
@app.get("/db/roster/ranges", dependencies=[Depends(verify_api_key)])
def list_roster_ranges(db: Session = Depends(get_db)):
    """Returns a list of unique date ranges present in the database, grouped by save batches."""
    from sqlalchemy import func
    
    # Group by created_at (the batch timestamp)
    batches = db.query(
        models_db.RosterAssignmentDB.created_at,
        func.min(models_db.RosterAssignmentDB.date).label("start"),
        func.max(models_db.RosterAssignmentDB.date).label("end")
    ).group_by(models_db.RosterAssignmentDB.created_at).order_by(models_db.RosterAssignmentDB.created_at.desc()).all()
    
    if not batches:
        return {"unique_ranges": []}
    
    ranges = []
    # Handle cases where created_at might be None (old data)
    none_batch = None
    
    for b in batches:
        if b.created_at is None:
            none_batch = b
            continue
        ranges.append({"start": b.start, "end": b.end})
    
    # Simple fallback for legacy data with no created_at
    if none_batch:
        legacy_dates = db.query(models_db.RosterAssignmentDB.date).filter(models_db.RosterAssignmentDB.created_at == None).distinct().order_by(models_db.RosterAssignmentDB.date).all()
        for ld in legacy_dates:
            ranges.append({"start": ld[0], "end": ld[0]})
            
    return {"unique_ranges": ranges}



class SingleAssignmentUpdate(BaseModel):
    staff_id: str
    date: str
    shift_type: str

@app.post("/db/roster/update", dependencies=[Depends(verify_api_key)])
def update_single_assignment(payload: SingleAssignmentUpdate, db: Session = Depends(get_db)):
    """Surgically updates or creates a single assignment in the database."""
    existing = db.query(models_db.RosterAssignmentDB).filter(
        models_db.RosterAssignmentDB.staff_id == payload.staff_id,
        models_db.RosterAssignmentDB.date == payload.date
    ).first()
    
    if existing:
        existing.shift_type = payload.shift_type
    else:
        new_row = models_db.RosterAssignmentDB(
            staff_id=payload.staff_id,
            date=payload.date,
            shift_type=payload.shift_type
        )
        db.add(new_row)
    
    db.commit()
    return {"status": "ok"}