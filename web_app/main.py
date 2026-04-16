# -*- coding: utf-8 -*-
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from runner import cleanup_loop, run_job

JOBS_DIR = os.path.join(os.path.dirname(__file__), "jobs")

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app):
    os.makedirs(JOBS_DIR, exist_ok=True)
    asyncio.create_task(cleanup_loop(JOBS_DIR))
    yield


app = FastAPI(title="Certificados Antecedentes Colombia", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SolicitudConsulta(BaseModel):
    cedula: str
    fecha_expedicion: str  # DD/MM/YYYY
    primer_nombre: str


@app.post("/api/submit")
@limiter.limit("5/minute")
async def submit(request: Request, body: SolicitudConsulta, background_tasks: BackgroundTasks):
    if not body.cedula.isdigit() or len(body.cedula) < 5:
        raise HTTPException(400, "La cédula debe ser numérica y tener al menos 5 dígitos")

    partes = body.fecha_expedicion.split("/")
    if len(partes) != 3 or not all(p.isdigit() for p in partes):
        raise HTTPException(400, "La fecha debe tener formato DD/MM/YYYY")

    if not body.primer_nombre.strip():
        raise HTTPException(400, "El primer nombre es requerido")

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    with open(os.path.join(job_dir, "status.json"), "w") as f:
        json.dump({"status": "processing", "errores": []}, f)

    background_tasks.add_task(run_job, job_id, job_dir, body.dict())
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/status/{job_id}")
async def status(job_id: str):
    ruta = os.path.join(JOBS_DIR, job_id, "status.json")
    if not os.path.exists(ruta):
        raise HTTPException(404, "Job no encontrado")
    with open(ruta, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("status") == "done":
        data["download_url"] = f"/api/download/{job_id}"
    return data


@app.get("/api/download/{job_id}")
async def download(job_id: str):
    job_dir = os.path.join(JOBS_DIR, job_id)
    if not os.path.isdir(job_dir):
        raise HTTPException(404, "Job no encontrado")
    zips = [f for f in os.listdir(job_dir) if f.endswith(".zip")]
    if not zips:
        raise HTTPException(404, "ZIP no disponible")
    return FileResponse(
        os.path.join(job_dir, zips[0]),
        media_type="application/zip",
        filename=zips[0],
    )


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")
