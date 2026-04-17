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
            await page.goto(URL_FORM, wait_until="load", timeout=60_000)
            await asyncio.sleep(2)

            await page.locator("select").first.select_option(label=label_tipo)
            await asyncio.sleep(0.3)

            campo = page.locator("#txtNumDoc")
            await campo.wait_for(state="visible", timeout=10_000)
            await campo.fill(cedula)
            await asyncio.sleep(0.3)

            # El servidor no valida el token de reCAPTCHA — se hace submit directo.
            async with context.expect_page(timeout=30_000) as nueva_pagina_info:
                await page.evaluate("""() => {
                    const form = document.forms['afiliado'];
                    form.__EVENTTARGET.value   = 'btnConsultar';
                    form.__EVENTARGUMENT.value = '';
                    form.submit();
                }""")

            resultado_page = await nueva_pagina_info.value
            await resultado_page.wait_for_load_state("load", timeout=30_000)
            await asyncio.sleep(2)

            # Verificar que la respuesta contiene los datos esperados
            contenido = await resultado_page.content()
            if "Resultados de la consulta" not in contenido:
                for frase_error in ["no se encontr", "error", "token inválido", "acceso denegado"]:
                    if frase_error in contenido.lower():
                        raise RuntimeError(f"ADRES devolvió error: '{frase_error}' en página de resultado")
                raise RuntimeError("ADRES: página de resultado no contiene datos esperados")

            ruta_pdf = os.path.join(output_dir, f"adres_{cedula}.pdf")
            await resultado_page.pdf(
                path=ruta_pdf,
                format="A4",
                print_background=True,
                margin={"top": "1cm", "bottom": "1cm", "left": "1.5cm", "right": "1.5cm"},
            )

            tamanio = os.path.getsize(ruta_pdf)
            if tamanio < TAMANIO_MINIMO_PDF:
                os.remove(ruta_pdf)
                raise RuntimeError(
                    f"PDF de ADRES sospechosamente pequeño ({tamanio} bytes)"
                )

            return ruta_pdf

        finally:
            await browser.close()


async def descargar(cedula: str, output_dir: str, tipo_doc: str = "CC") -> str:
    """Retorna ruta del PDF generado. Reintenta hasta 3 veces antes de lanzar excepción."""
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
