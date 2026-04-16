# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Descripción general

Dos capas de código para el mismo propósito — descargar certificados de antecedentes de 4 entidades gubernamentales colombianas:

- **Scripts raíz** (`descargar_*.py`): borradores standalone con `headless=False`, útiles para depurar selectores y flujos. Son la referencia cuando algo falla en la app.
- **`web_app/`**: aplicación FastAPI de producción con interfaz web, headless=True, y deploy en Railway vía Docker.

| Entidad | Módulo web_app | CAPTCHA |
|---------|---------------|---------|
| Policía — antecedentes judiciales | `scripts/antecedentes.py` | reCAPTCHA Enterprise (CapSolver) |
| Contraloría General | `scripts/contraloria.py` | reCAPTCHA v2 Enterprise (CapSolver) |
| Procuraduría General | `scripts/procuraduria.py` | Texto (resuelto localmente) |
| Policía — RNMC medidas correctivas | `scripts/medidas_correctivas.py` | Ninguno (requiere fecha de expedición) |

## Comandos

```bash
# Desarrollo local (desde web_app/)
cd web_app
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

# Prueba directa de un módulo sin levantar el servidor
cd web_app
python -c "
import asyncio
from scripts.medidas_correctivas import descargar
asyncio.run(descargar('1114480905', '05', '06', '2006', '/tmp/test'))
"

# Prueba paralela de los 4 módulos (simula lo que hace el servidor)
cd web_app
python -c "
import asyncio, os
from scripts import antecedentes, contraloria, procuraduria, medidas_correctivas
KEY = 'CAP-...'
OUT = '/tmp/test'
os.makedirs(OUT, exist_ok=True)
async def run():
    results = await asyncio.gather(
        antecedentes.descargar('CEDULA', OUT, KEY),
        contraloria.descargar('CEDULA', OUT, KEY),
        procuraduria.descargar('CEDULA', 'NOMBRE', OUT),
        medidas_correctivas.descargar('CEDULA', 'DD', 'MM', 'YYYY', OUT),
        return_exceptions=True
    )
    [print(n, ':', r) for n, r in zip(['ant','con','pro','med'], results)]
asyncio.run(run())
"

# Build Docker local
docker build -f web_app/Dockerfile -t hack-app .
docker run -p 8000:8000 -e CAPSOLVER_API_KEY=CAP-... hack-app
```

## Arquitectura web_app

```
web_app/
├── main.py          # FastAPI: rutas /api/submit, /api/status/{id}, /api/download/{id}
├── runner.py        # Orquesta las 4 descargas en paralelo (asyncio.gather + Semaphore(2))
├── scripts/         # Un módulo por entidad, cada uno expone descargar() async
└── static/          # Frontend vanilla JS: formulario → polling cada 3s → descarga ZIP
```

**Flujo de un job:**
1. `POST /api/submit` → crea UUID, guarda `jobs/{uuid}/status.json`, lanza `run_job()` en background
2. `run_job()` ejecuta los 4 `descargar()` en paralelo con timeout de 180s cada uno
3. Archivos exitosos se empaquetan en `certificados_{cedula}.zip`
4. `GET /api/status/{uuid}` devuelve el estado; cuando `"done"` incluye `download_url`
5. `GET /api/download/{uuid}` sirve el ZIP
6. Jobs se limpian automáticamente después de 2 horas (`cleanup_loop`)

**Variables de entorno (Railway):**
- `CAPSOLVER_API_KEY` — requerida para Policía y Contraloría
- `PORT` — inyectada automáticamente por Railway

## Quirks importantes por entidad

**Contraloría**: el formulario vive en un iframe de `cfiscal.contraloria.gov.co`. El módulo espera hasta 30s en loop para que el iframe aparezca antes de fallar.

**Procuraduría**: `resolver_captcha()` resuelve por regex 5 tipos de pregunta (matemáticas, dígitos de cédula, letras del nombre, capitales de departamento incluyendo "Colombia" → "Bogota"). Si llega una pregunta no reconocida lanza `ValueError`.

**RNMC (medidas correctivas)**: el sitio usa ASP.NET UpdatePanel. El botón "Consultar" aparece con id `btnConsultar2` al seleccionar Cédula (value=55). El click se hace con `__doPostBack('ctl00$ContentPlaceHolder3$btnConsultar2','')` directamente vía JS para evitar problemas con overlays y referencias stale del DOM.

**headless vs headed**: todos los módulos de `web_app/scripts/` usan `headless=True`. Los scripts raíz usan `headless=False` para depuración visual. `page.pdf()` solo funciona en headless; en headed cae a `page.screenshot()`.

## Deploy Railway

El `railway.toml` apunta al Dockerfile en `web_app/Dockerfile`. Railway construye desde la raíz del repo, por eso el Dockerfile usa `COPY web_app/requirements.txt .` y `COPY web_app/ .`. La imagen base es `mcr.microsoft.com/playwright/python:v{VERSION}-jammy` — versión debe coincidir con `playwright>=` en `requirements.txt`.
