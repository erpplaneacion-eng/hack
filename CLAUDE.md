# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Descripción general

Herramientas de automatización para descargar certificados de antecedentes de entidades gubernamentales colombianas. Stack: **Python + Playwright + playwright-stealth + CapSolver**.

| Script | Entidad | CAPTCHA |
|--------|---------|---------|
| `descargar_antecedentes.py` | Policía Nacional — antecedentes judiciales | reCAPTCHA Enterprise (CapSolver) |
| `descargar_contraloria.py` | Contraloría General | reCAPTCHA v2 Enterprise (CapSolver) |
| `descargar_procuraduria.py` | Procuraduría General | Captcha de texto (resuelto localmente) |
| `descargar_medidas_correctivas.py` | Policía Nacional — RNMC medidas correctivas | reCAPTCHA o imagen (auto-detección) |

## Instalación y ejecución

```bash
# Instalar dependencias base
pip install playwright capsolver playwright-stealth
playwright install chromium

# Opcional: OCR para captcha de imagen (medidas correctivas)
pip install ddddocr

# Ejecutar cada script
python descargar_antecedentes.py
python descargar_contraloria.py
python descargar_procuraduria.py
python descargar_medidas_correctivas.py
```

Los archivos generados (PNG, PDF, HTML de depuración) se guardan en `~/Downloads/antecedentes/`.

## Configuración

Cada script tiene una sección `# CONFIGURACION` al inicio con variables para modificar:

- `CAPSOLVER_API_KEY` — clave de la API de CapSolver (requerida en Policía y Contraloría)
- `CEDULA` — número de documento a consultar
- `PRIMER_NOMBRE` — usado por la Procuraduría para resolver su captcha
- `DIRECTORIO_DESCARGA` — carpeta de salida (por defecto `~/Downloads/antecedentes`)
- `headless` en `browser.launch()` — cambiar a `True` para obtener PDF directo vía Chromium

## Arquitectura

Todos los scripts siguen el mismo patrón asíncrono:

1. `async with async_playwright()` → lanza Chromium con flags anti-detección
2. `Stealth().apply_stealth_async(page)` → oculta huellas de automatización
3. Navegación + llenado de formulario
4. Resolución de CAPTCHA (CapSolver o lógica local)
5. Inyección del token / envío del formulario
6. Captura del resultado como PDF o PNG (fallback)

**Formularios en iframe**: Contraloría embebe el formulario en `cfiscal.contraloria.gov.co`. El script localiza el frame por URL con `page.frame(url="*cfiscal.contraloria.gov.co*")`.

**Captcha de la Procuraduría**: `resolver_captcha()` en `descargar_procuraduria.py` resuelve 5 tipos de preguntas mediante regex (operaciones matemáticas, dígitos del documento, letras del nombre, capitales de departamento). No requiere CapSolver.

**Modo headless y PDF**: `page.pdf()` solo funciona con `headless=True`. En modo visible (`headless=False`) el script cae al fallback de PNG automáticamente.
