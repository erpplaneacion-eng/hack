# -*- coding: utf-8 -*-
import asyncio
import json
import os
import shutil
import time
import zipfile

from scripts import antecedentes, contraloria, procuraduria, medidas_correctivas, adres

CAPSOLVER_API_KEY = os.getenv(
    "CAPSOLVER_API_KEY",
    "CAP-4630AE78E94762DF1C9E69E9DDACE200D1D979524936D4BA670B55E96585A975",
)
TIMEOUT_SEGUNDOS = 180

_semaforo = asyncio.Semaphore(3)
_status_lock = asyncio.Lock()

ENTIDADES = ["antecedentes", "contraloria", "procuraduria", "medidas_correctivas", "adres"]


async def _update_status(job_dir: str, **cambios):
    """Actualiza status.json de forma atómica con un lock global."""
    status_path = os.path.join(job_dir, "status.json")
    async with _status_lock:
        try:
            with open(status_path, encoding="utf-8") as f:
                estado = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            estado = {}

        for clave, valor in cambios.items():
            if clave == "resultado_entidad":
                entidad, datos = valor
                estado.setdefault("resultados", {})[entidad] = datos
            else:
                estado[clave] = valor

        with open(status_path, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False)


async def run_job(job_id: str, job_dir: str, params: dict):
    cedula = params["cedula"]
    partes = params["fecha_expedicion"].split("/")
    dia, mes, anio = partes[0], partes[1], partes[2]
    primer_nombre = params["primer_nombre"].upper().strip()

    # Inicializar estado con todas las entidades en "procesando"
    await _update_status(
        job_dir,
        overall_status="procesando",
        resultados={e: {"status": "procesando"} for e in ENTIDADES},
        errores=[],
        zip=None,
    )

    async def _run(coro, nombre):
        try:
            resultado = await asyncio.wait_for(coro, timeout=TIMEOUT_SEGUNDOS)
            return nombre, resultado, None
        except asyncio.TimeoutError:
            return nombre, None, f"timeout después de {TIMEOUT_SEGUNDOS}s"
        except Exception as e:
            return nombre, None, str(e)

    async with _semaforo:
        tareas = [
            asyncio.create_task(_run(antecedentes.descargar(cedula, job_dir, CAPSOLVER_API_KEY), "antecedentes")),
            asyncio.create_task(_run(contraloria.descargar(cedula, job_dir, CAPSOLVER_API_KEY), "contraloria")),
            asyncio.create_task(_run(procuraduria.descargar(cedula, primer_nombre, job_dir), "procuraduria")),
            asyncio.create_task(_run(medidas_correctivas.descargar(cedula, dia, mes, anio, job_dir), "medidas_correctivas")),
            asyncio.create_task(_run(adres.descargar(cedula, job_dir), "adres")),
        ]

        archivos = []
        errores = []

        for tarea in asyncio.as_completed(tareas):
            nombre, resultado, error = await tarea
            if error:
                errores.append({"entidad": nombre, "error": error})
                await _update_status(
                    job_dir,
                    resultado_entidad=(nombre, {"status": "error", "error": error}),
                    errores=errores,
                )
            elif resultado and os.path.exists(resultado):
                archivos.append(resultado)
                await _update_status(
                    job_dir,
                    resultado_entidad=(nombre, {
                        "status": "done",
                        "archivo": os.path.basename(resultado),
                    }),
                )

    # Empaquetar ZIP final
    zip_path = None
    if archivos:
        zip_name = f"certificados_{cedula}.zip"
        zip_path = os.path.join(job_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for archivo in archivos:
                zf.write(archivo, os.path.basename(archivo))

    overall = "done" if zip_path else "failed"
    await _update_status(
        job_dir,
        overall_status=overall,
        status=overall,  # backwards compat
        zip=os.path.basename(zip_path) if zip_path else None,
        archivos=[os.path.basename(a) for a in archivos],
        errores=errores,
    )


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
