from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.analyze import analyze, estimate_load
from app.core.engine import run as run_sim
from app.core.model import Architecture, load_architecture
from app.core.scenario import Scenario, load_scenario

router = APIRouter()
MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"


class SimulateRequest(BaseModel):
    architecture: Dict[str, Any] | str  # raw dict or model filename
    scenario: Dict[str, Any] | str      # raw dict or scenario filename
    seed: Optional[int] = None


class AnalyzeRequest(BaseModel):
    architecture: Dict[str, Any] | str


@router.get("/models")
def list_models():
    """List all available architecture templates."""
    models = []
    if MODELS_DIR.exists():
        for p in sorted(MODELS_DIR.glob("*.yaml")):
            try:
                arch = load_architecture(p)
                models.append({
                    "id": p.stem,
                    "name": arch.name,
                    "nodes": len(arch.nodes),
                    "edges": len(arch.edges),
                    "path": str(p.name),
                })
            except Exception:
                models.append({"id": p.stem, "name": p.stem, "path": str(p.name)})
    return {"models": models}


@router.get("/models/{model_id}")
def get_model(model_id: str):
    """Retrieve an architecture model definition."""
    path = MODELS_DIR / f"{model_id}.yaml"
    if not path.exists():
        path = MODELS_DIR / model_id
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    data = yaml.safe_load(path.read_text())
    return {"id": model_id, "data": data}


@router.get("/scenarios")
def list_scenarios():
    """List available failure scenarios."""
    scenarios = []
    if SCENARIOS_DIR.exists():
        for p in sorted(SCENARIOS_DIR.glob("*.yaml")):
            try:
                sc = load_scenario(p)
                scenarios.append({
                    "id": p.stem,
                    "name": sc.name,
                    "description": sc.description,
                    "duration_s": sc.duration_s,
                    "path": str(p.name),
                })
            except Exception:
                scenarios.append({"id": p.stem, "name": p.stem, "path": str(p.name)})
    return {"scenarios": scenarios}


@router.post("/analyze")
def run_analysis(req: AnalyzeRequest):
    """Run static flaw detection on an architecture."""
    if isinstance(req.architecture, str):
        path = MODELS_DIR / f"{req.architecture}.yaml"
        if not path.exists():
            path = MODELS_DIR / req.architecture
        if not path.exists():
            raise HTTPException(status_code=404, detail="Model file not found")
        arch = load_architecture(path)
    else:
        arch = Architecture.model_validate(req.architecture)

    findings = analyze(arch)
    load = estimate_load(arch)

    return {
        "architecture": arch.name,
        "findings": [
            {
                "rule": f.rule,
                "severity": f.severity,
                "target": f.target,
                "message": f.message,
                "fix": f.fix,
            }
            for f in findings
        ],
        "load_estimate": {
            "rps": load.rps,
            "hold_ms": load.hold_ms,
            "utilization": {k: round(v, 4) for k, v in load.util.items()},
        },
    }


@router.post("/simulate")
def run_simulation(req: SimulateRequest):
    """Run discrete-event simulation of an architecture under a scenario."""
    # Resolve architecture
    if isinstance(req.architecture, str):
        path = MODELS_DIR / f"{req.architecture}.yaml"
        if not path.exists():
            path = MODELS_DIR / req.architecture
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Architecture '{req.architecture}' not found")
        arch = load_architecture(path)
    else:
        arch = Architecture.model_validate(req.architecture)

    # Resolve scenario
    if isinstance(req.scenario, str):
        path = SCENARIOS_DIR / f"{req.scenario}.yaml"
        if not path.exists():
            path = SCENARIOS_DIR / req.scenario
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Scenario '{req.scenario}' not found")
        sc = load_scenario(path)
    else:
        sc = Scenario.model_validate(req.scenario)

    if req.seed is not None:
        sc.seed = req.seed

    summary = run_sim(arch, sc)

    return {
        "architecture": summary.architecture,
        "scenario": summary.scenario,
        "status": summary.status,
        "requests": summary.requests,
        "error_rate": summary.error_rate,
        "p50_ms": summary.p50_ms,
        "p99_ms": summary.p99_ms,
        "recovery_s": summary.recovery_s,
        "wall_time_s": summary.wall_time_s,
        "client_errors": dict(summary.client_errors),
        "node_errors": {k: dict(v) for k, v in summary.node_errors.items() if v},
        "cascade": summary.cascade,
        "slo_breached": summary.slo_breached,
    }
