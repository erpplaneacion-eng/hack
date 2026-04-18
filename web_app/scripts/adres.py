# -*- coding: utf-8 -*-
import asyncio
import os

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_FORM = "https://aplicaciones.adres.gov.co/BDUA_Internet/Pages/ConsultarAfiliadoWeb_2.aspx"

TIPOS_DOC = {
    "CC": "Cedula de Ciudadanía",
    "TI": "Tarjeta de Identidad",
    "CE": "Cedula de Extranjería",
    "PA": "Pasaporte",
    "RC": "Registro Civil",
}

TAMANIO_MINIMO_PDF = 10_000
FRASE_EXITO = "Resultados de la consulta"
FRASES_ERROR = ["no se encontr", "token inválido", "acceso denegado"]


async def _esperar_contenido(page_obj, timeout_ms: int = 20_000) -> bool:
    """Espera hasta que el texto de éxito aparezca en el DOM. Retorna True si lo encontró."""
    intervalo = 1_000
    intentos = timeout_ms // intervalo
    for _ in range(intentos):
        try:
            contenido = await page_obj.content()
            if FRASE_EXITO in contenido:
                return True
        except Exception:
            pass
        await asyncio.sleep(intervalo / 1_000)
    return False


async def _guardar_pdf(page_obj, ruta_pdf: str) -> str:
    """Intenta PDF; cae a PNG si falla. Valida tamaño mínimo."""
    try:
        await page_obj.pdf(
            path=ruta_pdf,
            format="A4",
            print_background=True,
            margin={"top": "1cm", "bottom": "1cm", "left": "1.5cm", "right": "1.5cm"},
        )
        tamanio = os.path.getsize(ruta_pdf)
        if tamanio < TAMANIO_MINIMO_PDF:
            os.remove(ruta_pdf)
            raise RuntimeError(f"PDF sospechosamente pequeño ({tamanio} bytes)")
        return ruta_pdf
    except Exception as e_pdf:
        ruta_png = ruta_pdf.replace(".pdf", ".png")
        await page_obj.screenshot(path=ruta_png, full_page=True)
        tamanio = os.path.getsize(ruta_png)
        if tamanio < TAMANIO_MINIMO_PDF:
            os.remove(ruta_png)
            raise RuntimeError(f"PNG también sospechosamente pequeño ({tamanio} bytes). PDF error: {e_pdf}")
        return ruta_png


async def _intentar_descarga(cedula: str, output_dir: str, tipo_doc: str = "CC") -> str:
    label_tipo = TIPOS_DOC.get(tipo_doc, "Cedula de Ciudadanía")

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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            await page.goto(URL_FORM, wait_until="domcontentloaded", timeout=60_000)
            await asyncio.sleep(2)

            await page.locator("select").first.select_option(label=label_tipo)
            await asyncio.sleep(0.3)

            campo = page.locator("#txtNumDoc")
            await campo.wait_for(state="visible", timeout=10_000)
            await campo.fill(cedula)
            await asyncio.sleep(0.3)

            # Intentar capturar el popup que abre el formulario
            resultado_page = None
            try:
                async with context.expect_page(timeout=25_000) as nueva_pagina_info:
                    await page.evaluate("""() => {
                        const form = document.forms['afiliado'];
                        form.__EVENTTARGET.value   = 'btnConsultar';
                        form.__EVENTARGUMENT.value = '';
                        form.submit();
                    }""")
                resultado_page = await nueva_pagina_info.value
                # Solo esperar domcontentloaded — "load" nunca llega en este portal
                try:
                    await resultado_page.wait_for_load_state("domcontentloaded", timeout=20_000)
                except PlaywrightTimeout:
                    pass  # Si ya disparó, el DOM está disponible de todas formas
                await asyncio.sleep(3)
            except PlaywrightTimeout:
                # El popup no abrió — puede que el resultado haya cargado en la misma página
                resultado_page = None

            # Fallback 1: resultado en la misma página (sin popup)
            if resultado_page is None:
                await page.wait_for_load_state("domcontentloaded", timeout=20_000)
                await asyncio.sleep(3)
                if await _esperar_contenido(page, timeout_ms=15_000):
                    resultado_page = page
                else:
                    raise RuntimeError("ADRES no abrió popup ni cargó resultado en la página principal")

            # Fallback 2: esperar activamente a que aparezca el texto de éxito en el popup
            if FRASE_EXITO not in await resultado_page.content():
                encontrado = await _esperar_contenido(resultado_page, timeout_ms=20_000)
                if not encontrado:
                    contenido = await resultado_page.content()
                    for frase in FRASES_ERROR:
                        if frase in contenido.lower():
                            raise RuntimeError(f"ADRES devolvió error: '{frase}' en página de resultado")
                    raise RuntimeError("ADRES: página de resultado no contiene datos esperados tras espera activa")

            ruta_pdf = os.path.join(output_dir, f"adres_{cedula}.pdf")
            return await _guardar_pdf(resultado_page, ruta_pdf)

        finally:
            await browser.close()


async def descargar(cedula: str, output_dir: str, tipo_doc: str = "CC") -> str:
    """Retorna ruta del PDF (o PNG) generado. Reintenta hasta 3 veces antes de lanzar excepción."""
    os.makedirs(output_dir, exist_ok=True)

    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 4):
        try:
            return await _intentar_descarga(cedula, output_dir, tipo_doc)
        except Exception as e:
            ultimo_error = e
            if intento < 3:
                await asyncio.sleep(3 * intento)

    raise RuntimeError(f"ADRES falló tras 3 intentos. Último error: {ultimo_error}")
