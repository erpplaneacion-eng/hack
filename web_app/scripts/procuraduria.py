# -*- coding: utf-8 -*-
import asyncio
import os
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_IFRAME = "https://apps.procuraduria.gov.co/webcert/inicio.aspx?tpo=2"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

PREGUNTAS_GEOGRAFIA = {
    "atlantico": "Barranquilla", "antioquia": "Medellin", "colombia": "Bogota",
    "cundinamarca": "Bogota", "valle del cauca": "Cali", "bolivar": "Cartagena",
    "santander": "Bucaramanga", "narino": "Pasto", "cordoba": "Monteria",
    "caldas": "Manizales", "risaralda": "Pereira", "quindio": "Armenia",
    "huila": "Neiva", "tolima": "Ibague", "cauca": "Popayan",
    "cesar": "Valledupar", "choco": "Quibdo", "boyaca": "Tunja",
    "meta": "Villavicencio", "magdalena": "Santa Marta", "sucre": "Sincelejo",
    "guajira": "Riohacha", "norte de santander": "Cucuta", "arauca": "Arauca",
    "casanare": "Yopal", "vichada": "Puerto Carreno", "guainia": "Inirida",
    "vaupes": "Mitu", "amazonas": "Leticia", "putumayo": "Mocoa",
    "caqueta": "Florencia", "guaviare": "San Jose del Guaviare",
    "archipielago de san andres": "San Andres",
}


def resolver_captcha(texto: str, cedula: str, primer_nombre: str) -> str:
    texto_lower = texto.lower().strip()
    PALABRAS = {"dos": 2, "tres": 3, "cuatro": 4, "cinco": 5}

    def _n(texto_buscar, default):
        m = re.search(r'(dos|tres|cuatro|cinco|\d+)', texto_buscar)
        if not m:
            return default
        v = m.group(1)
        return PALABRAS[v] if v in PALABRAS else int(v)

    if "letras" in texto_lower and "nombre" in texto_lower:
        return str(len(primer_nombre.replace(" ", "")))

    if "nombre" in texto_lower and ("primeros" in texto_lower or "digitos" in texto_lower or "letras" in texto_lower):
        return primer_nombre[:_n(texto_lower, 3)]

    if "ultimos" in texto_lower and ("digitos" in texto_lower or "documento" in texto_lower):
        return cedula[-_n(texto_lower, 2):]

    if ("primeros" in texto_lower or "digitos" in texto_lower) and ("documento" in texto_lower or "cedula" in texto_lower or "consultar" in texto_lower):
        return cedula[:_n(texto_lower, 3)]

    match_op = re.search(r'(\d+)\s*([+\-*/xX])\s*(\d+)', texto)
    if match_op:
        a, op, b = int(match_op.group(1)), match_op.group(2), int(match_op.group(3))
        if op == '+':             return str(a + b)
        if op == '-':             return str(a - b)
        if op in ('*', 'x', 'X'): return str(a * b)

    if "capital" in texto_lower:
        for depto, capital in PREGUNTAS_GEOGRAFIA.items():
            depto_norm = (depto.replace("a", "[aá]").replace("e", "[eé]")
                          .replace("i", "[ií]").replace("o", "[oó]")
                          .replace("u", "[uú]").replace("n", "[nñ]"))
            if re.search(depto_norm, texto_lower, re.IGNORECASE):
                return capital

    raise ValueError(f"Tipo de captcha no reconocido: '{texto}'")


_ERRORES_PAGINA = [
    "captcha incorrecto", "código de verificación", "no se encontr",
    "error al procesar", "intente nuevamente", "datos incorrectos",
]
TAMANIO_MINIMO_PDF = 10_000


async def _intentar_descarga(cedula: str, primer_nombre: str, output_dir: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            await page.goto(URL_IFRAME, wait_until="domcontentloaded", timeout=30_000)

            await page.locator("#ddlTipoID").select_option(value="1")

            # Buscar campo de cédula (múltiples selectores posibles)
            campo_cedula = None
            for selector in ["#txtNumDoc", "#numDoc", "input[name*='numDoc']",
                             "input[name*='cedula']", "input[name*='documento']",
                             "input[type='text']"]:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0 and await el.is_visible():
                        campo_cedula = el
                        break
                except Exception:
                    continue

            if campo_cedula is None:
                raise RuntimeError("No se encontró campo de cédula en Procuraduría")

            await campo_cedula.fill(cedula)

            # Click en primer radio si existe
            radios = await page.locator("input[type='radio']:visible").all()
            if radios:
                await radios[0].click()

            # CAPTCHA de texto (resolución local)
            texto_captcha = await page.locator("#lblPregunta").inner_text(timeout=8_000)
            if not texto_captcha:
                raise RuntimeError("No se encontró texto del captcha en Procuraduría")
            respuesta = resolver_captcha(texto_captcha, cedula, primer_nombre.upper())

            # Buscar campo del captcha
            campo_captcha = None
            for selector in ["#txtCaptcha", "#captcha", "input[name*='captcha']",
                             "input[name*='Captcha']", "input[name*='codigo']"]:
                try:
                    el = page.locator(selector).first
                    if await el.count() > 0 and await el.is_visible():
                        campo_captcha = el
                        break
                except Exception:
                    continue

            if campo_captcha is None:
                todos_inputs = await page.locator("input[type='text']:visible").all()
                if todos_inputs:
                    campo_captcha = todos_inputs[-1]

            if campo_captcha is None:
                raise RuntimeError("No se encontró campo de captcha en Procuraduría")

            await campo_captcha.fill(respuesta)
            await page.locator("#btnExportar").click(timeout=8_000, force=True)

            # Esperar #btnDescargar — el server puede tardar hasta 60s, no se puede acelerar
            try:
                await page.locator("#btnDescargar").wait_for(state="visible", timeout=60_000)
            except PlaywrightTimeout:
                contenido = (await page.content()).lower()
                for frase in _ERRORES_PAGINA:
                    if frase in contenido:
                        raise RuntimeError(f"Procuraduría rechazó la consulta: '{frase}' en página")
                raise RuntimeError("Timeout esperando #btnDescargar en Procuraduría")

            # Verificar que no hay error de captcha (el botón puede aparecer junto al error)
            contenido = (await page.content()).lower()
            for frase in _ERRORES_PAGINA:
                if frase in contenido:
                    raise RuntimeError(f"Procuraduría devolvió error: '{frase}' en página")

            async with page.expect_download(timeout=30_000) as dl_info:
                await page.locator("#btnDescargar").click(timeout=8_000)
            descarga = await dl_info.value
            nombre = descarga.suggested_filename or f"procuraduria_{cedula}.pdf"
            ruta_final = os.path.join(output_dir, nombre)
            await descarga.save_as(ruta_final)

            tamanio = os.path.getsize(ruta_final)
            if tamanio < TAMANIO_MINIMO_PDF:
                os.remove(ruta_final)
                raise RuntimeError(
                    f"El archivo descargado es sospechosamente pequeño ({tamanio} bytes), "
                    "probablemente es una página de error"
                )
            return ruta_final
        finally:
            await browser.close()


async def descargar(cedula: str, primer_nombre: str, output_dir: str) -> str:
    """Retorna ruta del archivo generado. Reintenta hasta 2 veces antes de lanzar."""
    os.makedirs(output_dir, exist_ok=True)

    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 3):  # 2 intentos en lugar de 3
        try:
            return await _intentar_descarga(cedula, primer_nombre, output_dir)
        except Exception as e:
            ultimo_error = e
            if intento < 2:
                await asyncio.sleep(2)

    raise RuntimeError(f"Procuraduría falló tras 2 intentos. Último error: {ultimo_error}")
