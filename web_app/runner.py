# -*- coding: utf-8 -*-
import asyncio
import json
import os
import shutil
import time
import zipfile

from scripts import antecedentes, contraloria, procuraduria, medidas_correctivas

CAPSOLVER_API_KEY = os.getenv(
    "CAPSOLVER_API_KEY",
    "CAP-4630AE78E94762DF1C9E69E9DDACE200D1D979524936D4BA670B55E96585A975",
)
TIMEOUT_SEGUNDOS = 180

_semaforo = asyncio.Semaphore(2)


async def run_job(job_id: str, job_dir: str, params: dict):
    cedula = params["cedula"]
    partes = params["fecha_expedicion"].split("/")
    dia, mes, anio = partes[0], partes[1], partes[2]
    primer_nombre = params["primer_nombre"].upper().strip()

    async def _run(coro, nombre):
        try:
            return await asyncio.wait_for(coro, timeout=TIMEOUT_SEGUNDOS)
        except asyncio.TimeoutError:
            return Exception(f"timeout después de {TIMEOUT_SEGUNDOS}s")
        except Exception as e:
            return e

    async with _semaforo:
        resultados = await asyncio.gather(
            _run(antecedentes.descargar(cedula, job_dir, CAPSOLVER_API_KEY), "antecedentes"),
            _run(contraloria.descargar(cedula, job_dir, CAPSOLVER_API_KEY), "contraloria"),
            _run(procuraduria.descargar(cedula, primer_nombre, job_dir), "procuraduria"),
            _run(medidas_correctivas.descargar(cedula, dia, mes, anio, job_dir), "medidas_correctivas"),
        )

    nombres = ["antecedentes", "contraloria", "procuraduria", "medidas_correctivas"]
    errores = []
    archivos = []

    for nombre, resultado in zip(nombres, resultados):
        if isinstance(resultado, Exception):
            errores.append({"entidad": nombre, "error": str(resultado)})
        elif resultado and os.path.exists(resultado):
            archivos.append(resultado)

    zip_path = None
    if archivos:
        zip_name = f"certificados_{cedula}.zip"
        zip_path = os.path.join(job_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for archivo in archivos:
                zf.write(archivo, os.path.basename(archivo))

    estado = {
        "status": "done" if zip_path else "failed",
        "zip": os.path.basename(zip_path) if zip_path else None,
        "archivos": [os.path.basename(a) for a in archivos],
        "errores": errores,
    }
    with open(os.path.join(job_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False)


async def cleanup_loop(jobs_dir: str):
    while True:
        await asyncio.sleep(3600)
        ahora = time.time()
        try:
            for entry in os.scandir(jobs_dir):
                if entry.is_dir() and ahora - entry.stat().st_mtime > 7200:
                    shutil.rmtree(entry.path, ignore_errors=True)
        except Exception:
            pass
