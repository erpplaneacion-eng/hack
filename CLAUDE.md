# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Descripción general

Dos capas de código para el mismo propósito — descargar certificados de antecedentes de 5 entidades gubernamentales colombianas:

- **`scripts/descargar_*.py`**: borradores standalone con `headless=False`, útiles para depurar selectores y flujos. Son la referencia cuando algo falla en la app.
- **`web_app/`**: aplicación FastAPI de producción con interfaz web, headless=True, y deploy en Railway vía Docker.

| Entidad | Módulo web_app | CAPTCHA | Reintentos |
|---------|---------------|---------|------------|
| Policía — antecedentes judiciales | `scripts/antecedentes.py` | reCAPTCHA Enterprise (CapSolver, lanzado en paralelo al `goto`, timeout 90s) | 3 intentos, espera 3s |
| Contraloría General | `scripts/contraloria.py` | reCAPTCHA v2 Enterprise (CapSolver, lanzado en paralelo al `goto`, timeout 90s) | 3 intentos, espera 3s |
| Procuraduría General | `scripts/procuraduria.py` | Texto (resuelto localmente) | 2 intentos, espera 2s |
| Policía — RNMC medidas correctivas | `scripts/medidas_correctivas.py` | Ninguno (requiere fecha de expedición) | 2 intentos |
| ADRES — afiliación EPS/BDUA | `scripts/adres.py` | reCAPTCHA (submit directo, sin validar token) | 3 intentos, espera progresiva |
| RUAF — Registro Único de Afiliados | `scripts/ruaf.py` | Imagen/OCR (CapSolver `ImageToTextTask`, hasta 4 intentos internos de captcha) | 3 intentos, espera 3s — **EN PRUEBAS, descarga no garantizada** |

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

# Prueba paralela de los 5 módulos (simula lo que hace el servidor)
cd web_app
python -c "
import asyncio, os
from scripts import antecedentes, contraloria, procuraduria, medidas_correctivas, adres
KEY = 'CAP-...'
OUT = '/tmp/test'
os.makedirs(OUT, exist_ok=True)
async def run():
    results = await asyncio.gather(
        antecedentes.descargar('CEDULA', OUT, KEY),
        contraloria.descargar('CEDULA', OUT, KEY),
        procuraduria.descargar('CEDULA', 'NOMBRE', OUT),
        medidas_correctivas.descargar('CEDULA', 'DD', 'MM', 'YYYY', OUT),
        adres.descargar('CEDULA', OUT),
        return_exceptions=True
    )
    [print(n, ':', r) for n, r in zip(['ant','con','pro','med','adres'], results)]
asyncio.run(run())
"

# Build Docker local
docker build -f web_app/Dockerfile -t hack-app .
docker run -p 8000:8000 -e CAPSOLVER_API_KEY=CAP-... hack-app
```

## Arquitectura web_app

```
web_app/
├── main.py          # FastAPI: /api/submit, /api/status/{id}, /api/download/{id}, /api/download/{id}/{entidad}
├── runner.py        # Orquesta las 6 descargas con asyncio.as_completed + escritura incremental de status.json
├── scripts/         # Un módulo por entidad, cada uno expone descargar() async
└── static/          # Frontend vanilla JS: formulario → polling cada 1.5s → estado granular + descarga individual o ZIP + mini-juego "Atrapa los certificados" mientras se espera
```

**Flujo de un job:**
1. `POST /api/submit` → valida cédula/fecha/nombre, crea UUID, inicializa `jobs/{uuid}/status.json` con todas las entidades en `"procesando"`, lanza `run_job()` en background
2. `run_job()` ejecuta los 6 `descargar()` en paralelo con timeout de 300s cada uno (Semaphore(3) limita jobs concurrentes). Usa `asyncio.as_completed` para escribir el estado de cada entidad apenas termina, no al final
3. `_update_status()` con `asyncio.Lock` garantiza escrituras atómicas a `status.json`
4. Archivos exitosos se empaquetan en `certificados_{cedula}.zip` cuando todas las entidades terminan
5. `GET /api/status/{uuid}` devuelve estado con `overall_status`, `resultados[entidad].status` y `download_url` por entidad lista
6. `GET /api/download/{uuid}` sirve el ZIP completo; `GET /api/download/{uuid}/{entidad}` sirve el PDF/PNG individual
7. Jobs se limpian automáticamente después de 2 horas (`cleanup_loop`)

**Estructura de `status.json`:**
```json
{
  "overall_status": "procesando|done|failed",
  "status": "procesando|done|failed",
  "resultados": {
    "antecedentes":        {"status": "procesando|done|error", "archivo": "...", "error": "..."},
    "contraloria":         {...},
    "procuraduria":        {...},
    "medidas_correctivas": {...},
    "adres":               {...}
  },
  "zip": "certificados_{cedula}.zip",
  "errores": [{"entidad": "...", "error": "..."}]
}
```

`status` es alias de `overall_status` por retrocompatibilidad con clientes viejos — `main.py` y `runner.py` lo escriben en paralelo.

**Rate limiting:** `POST /api/submit` está limitado a 5 req/min por IP via `slowapi`.

**Selector de entidades:** El frontend permite marcar/desmarcar entidades antes de enviar. El payload de `/api/submit` incluye `entidades: list[str]`; `main.py` filtra las tareas en `runner.py` según la lista recibida. RUAF viene **desmarcado por defecto** (label "en prueba") porque su captcha OCR no es fiable — es opt-in explícito.

**Variables de entorno (Railway):**
- `CAPSOLVER_API_KEY` — usada por Policía y Contraloría. `runner.py` tiene un fallback hardcoded; en producción debe inyectarse desde Railway.
- `TWOCAPTCHA_API_KEY` — fallback de CAPTCHA para Policía y Contraloría. Si CapSolver agota sus 3 tipos de tarea sin token, `scripts/_captcha.py` llama a 2captcha vía REST. Sin esta variable el fallback no activa y el comportamiento es igual al anterior. Obtener key en https://2captcha.com.
- `LANDIGAI_API_KEY` — OCR alternativo para RUAF vía LandingAI (en desarrollo).
- `LANDIGAI_API_URL` — URL del endpoint de LandingAI (por defecto `https://api.va.landing.ai/v1/ade/parse`).
- `PORT` — inyectada automáticamente por Railway

## Firma de cada descargar()

```python
antecedentes.descargar(cedula, output_dir, capsolver_api_key) -> str   # PDF o PNG
contraloria.descargar(cedula, output_dir, capsolver_api_key)  -> str   # PDF o PNG
procuraduria.descargar(cedula, primer_nombre, output_dir)     -> str   # PDF (nombre sugerido por el servidor)
medidas_correctivas.descargar(cedula, dia, mes, anio, output_dir) -> str  # PDF o PNG
adres.descargar(cedula, output_dir, tipo_doc="CC")            -> str   # PDF
ruaf.descargar(cedula, dia, mes, anio, output_dir, capsolver_api_key, landigai_key, landigai_url) -> str  # PDF o PNG — EN PRUEBAS
```

Todas retornan la ruta absoluta del archivo generado o lanzan `RuntimeError`.

**Nombres de archivo de salida:**
- `antecedentes_{cedula}.pdf` / `.png`
- `contraloria_{cedula}.pdf` / `.png` (o nombre dado por servidor)
- `procuraduria_{cedula}.pdf` (nombre sugerido por descarga del servidor)
- `medidas_correctivas_{cedula}.pdf` / `.png` (o nombre dado por servidor)
- `adres_{cedula}.pdf`
- `ruaf_{cedula}.pdf` / `.png` — EN PRUEBAS

## Quirks importantes por entidad

**Antecedentes / Contraloría (robustez)**: ambos módulos tienen 3 reintentos externos (`descargar()`) con `sleep(3)` entre ellos. La resolución de CAPTCHA está centralizada en `scripts/_captcha.py` → `resolver_recaptcha(sitekey, url, capsolver_key, page_action)`. Flujo: CapSolver prueba 3 tipos de tarea (`ReCaptchaV2Enterprise`, `ReCaptchaV3`, `ReCaptchaV2`) con 3 intentos cada uno; si agota todos sin token, cae a 2captcha vía REST (`TWOCAPTCHA_API_KEY`). Timeout del captcha subido de 60/90s a 120s para dar margen al fallback. En `_inyectar_token()` de antecedentes se añadió la guarda `___grecaptcha_cfg.clients &&` antes de `Object.entries()` para evitar `TypeError` cuando el widget de reCAPTCHA aún no ha inicializado `.clients` al momento de la inyección (race condition entre CapSolver rápido y carga lenta del servidor colombiano).

**Contraloría**: el formulario vive en un iframe de `cfiscal.contraloria.gov.co`. La detección del frame usa `page.frame_locator("iframe[src*='cfiscal']")` + `wait_for` del elemento interno `#ddlTipoDocumento`, lo que garantiza que el frame ya está registrado en `page.frames` cuando se intenta acceder (reemplazó al loop `sleep(0.25) × 40` que tenía una race condition).

**Procuraduría**: `resolver_captcha()` resuelve por regex 5 tipos de pregunta (matemáticas, dígitos de cédula, letras del nombre, capitales de departamento incluyendo "Colombia" → "Bogota"). Si llega una pregunta no reconocida lanza `ValueError`. Espera `#btnDescargar` (timeout 60s — server-side, no se puede acelerar) para confirmar éxito antes de intentar descargar.

**RNMC (medidas correctivas)**: el sitio usa ASP.NET UpdatePanel. El botón "Consultar" aparece con id `btnConsultar2` al seleccionar Cédula (value=55). El click se hace con `__doPostBack('ctl00$ContentPlaceHolder3$btnConsultar2','')` directamente vía JS para evitar problemas con overlays y referencias stale del DOM. La sincronización del AJAX se hace con `wait_for_function` esperando que `.loader_decad` se oculte y `__doPostBack` esté definido.

**ADRES**: el reCAPTCHA no se valida en servidor — se hace submit directo via `form.__EVENTTARGET`. La página de resultado debe contener "Resultados de la consulta" para considerarse válida. PDFs menores a 10 KB se descartan como inválidos.

**Antecedentes / Contraloría**: validan tamaño de PDF (≥ 10 KB) y presencia de errores en la página antes de aceptar el resultado, igual que ADRES y Procuraduría. Cada etapa imprime `[entidad:cedula] <etapa>` a stdout para diagnosticar fallas en logs de Railway.

**RUAF** (`ruaf.sispro.gov.co`): flujo en dos páginas — primero acepta términos en `TerminosCondiciones.aspx` (radio `input[type='radio']:first-of-type` + botón `input[value='Enviar']`), luego llena el formulario en `Filtro.aspx`. IDs exactos confirmados: dropdown `#ddlTiposDocumentos` (value `"5|CC"` para cédula), captcha input `#MainContent_txtCaptcha`, botón verificar `#MainContent_btnVerify` (texto "Verificar", NO "Validar"), botón consultar `#MainContent_btnConsultar`. La imagen del captcha se obtiene via `fetch` con `cache:'no-store'` desde JS para forzar imagen fresca del servidor en cada intento (evita que el browser cache la URL estática). El indicador de captcha correcto es `btnConsultar.is_enabled()` — si sigue deshabilitado (`aspNetDisabled`), el captcha fue rechazado. **Estado: EN PRUEBAS** — el OCR de CapSolver (`ImageToTextTask`) aún no resuelve el captcha con consistencia; pendiente ajuste de selector de imagen y validación del formato del captcha. Guarda `ruaf_captcha_{cedula}.png` en el directorio del job para diagnóstico.

**headless vs headed**: todos los módulos de `web_app/scripts/` usan `headless=True`. Los scripts raíz usan `headless=False` para depuración visual. `page.pdf()` solo funciona en headless; en headed cae a `page.screenshot()`.

## Optimizaciones de rendimiento

Tiempo total bajó de ~90s a ~40s aplicando 3 patrones simultáneos:

**1. Bloqueo selectivo de recursos** — `context.route()` aborta requests por `resource_type`. La regla difiere según cómo se obtiene el PDF:
- Scripts que descargan el PDF directo del servidor (`procuraduria.py`, `adres.py`): bloquean `image`, `media`, `font` (no afecta el PDF descargado).
- Scripts que generan el PDF con `page.pdf()` desde la página renderizada (`antecedentes.py`, `contraloria.py`, `medidas_correctivas.py`): bloquean **sólo `media`**, porque imágenes y fuentes son necesarias para que aparezcan logos y tipografía.

**2. CapSolver → 2captcha lanzado en paralelo** — en `antecedentes.py` y `contraloria.py` la sitekey es constante. Se llama `asyncio.create_task(asyncio.to_thread(resolver_recaptcha, ...))` ANTES del `browser.launch()` y se hace `await captcha_task` justo antes de inyectar el token. Convierte ~10s secuenciales en `max(navegación, captcha)`. La lógica de fallback vive en `scripts/_captcha.py`.

**3. `wait_for_function` en lugar de `sleep` + polling** — todos los scripts reemplazan `wait_until="networkidle"` y bloques de `asyncio.sleep()` por:
- `wait_until="domcontentloaded"` en los `goto`
- `page.wait_for_function(...)` que termina apenas el DOM cambia (típicamente 50-200ms vs sleep fijo de 1-3s)

**Streaming al frontend**: `runner.py` usa `asyncio.as_completed` y `_update_status()` con lock para escribir `status.json` cada vez que una entidad termina. El frontend hace polling cada 1.5s y muestra checkmarks/botones de descarga uno a uno, en lugar de un spinner ciego de 40s.

## Deploy Railway

El `railway.toml` apunta al Dockerfile en `web_app/Dockerfile`. Railway construye desde la raíz del repo, por eso el Dockerfile usa `COPY web_app/requirements.txt .` y `COPY web_app/ .`. La imagen base es `mcr.microsoft.com/playwright/python:v1.59.0-jammy` y `requirements.txt` pide `playwright>=1.59.0`. Si actualizas una, actualiza la otra.
