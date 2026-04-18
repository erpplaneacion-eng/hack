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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def _guardar_pdf(page_obj, ruta_pdf: str) -> str:
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
            raise RuntimeError(f"PNG también pequeño ({tamanio} bytes). PDF error: {e_pdf}")
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
                "--disable-extensions",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        # Bloquear sólo media — imágenes y fuentes son necesarias para el PDF
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type == "media"
            else route.continue_(),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            # 1. Cargar formulario — domcontentloaded es suficiente, no esperamos load
            await page.goto(URL_FORM, wait_until="domcontentloaded", timeout=30_000)

            # 2. Llenar — sin sleeps, los wait_for/fill son sincrónicos con el DOM
            await page.locator("select").first.select_option(label=label_tipo)
            campo = page.locator("#txtNumDoc")
            await campo.wait_for(state="attached", timeout=5_000)
            await campo.fill(cedula)

            # 3. Submit y capturar popup
            async with context.expect_page(timeout=20_000) as nueva_pagina_info:
                await page.evaluate("""() => {
                    const form = document.forms['afiliado'];
                    form.__EVENTTARGET.value   = 'btnConsultar';
                    form.__EVENTARGUMENT.value = '';
                    form.submit();
                }""")
            resultado_page = await nueva_pagina_info.value

            # En la página de resultado NO bloqueamos imágenes ni fuentes para que el PDF
            # quede bien renderizado (logos, tipografía oficial). Sólo media.
            await resultado_page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type == "media"
                else route.continue_(),
            )

            # 4. Esperar a que aparezca el texto de éxito O un error — termina apenas suceda
            try:
                await resultado_page.wait_for_function(
                    f"""() => {{
                        const t = document.body.innerText.toLowerCase();
                        return t.includes('{FRASE_EXITO.lower()}') ||
                               t.includes('no se encontr') ||
                               t.includes('token inválido') ||
                               t.includes('acceso denegado');
                    }}""",
                    timeout=25_000,
                )
            except PlaywrightTimeout:
                raise RuntimeError("ADRES: timeout esperando contenido en página de resultado")

            # 5. Verificar que sea éxito y no error
            contenido = (await resultado_page.content()).lower()
            for frase in FRASES_ERROR:
                if frase in contenido:
                    raise RuntimeError(f"ADRES devolvió error: '{frase}'")
            if FRASE_EXITO.lower() not in contenido:
                raise RuntimeError("ADRES: página sin frase de éxito tras espera")

            # 6. Esperar que terminen las imágenes/fuentes antes del PDF (evita layout roto)
            try:
                await resultado_page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeout:
                pass

            ruta_pdf = os.path.join(output_dir, f"adres_{cedula}.pdf")
            return await _guardar_pdf(resultado_page, ruta_pdf)

        finally:
            await browser.close()


async def descargar(cedula: str, output_dir: str, tipo_doc: str = "CC") -> str:
    """Retorna ruta del PDF (o PNG) generado. Reintenta hasta 2 veces antes de lanzar excepción."""
    os.makedirs(output_dir, exist_ok=True)

    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 3):  # 2 intentos en lugar de 3
        try:
            return await _intentar_descarga(cedula, output_dir, tipo_doc)
        except Exception as e:
            ultimo_error = e
            if intento < 2:
                await asyncio.sleep(2)  # delay corto entre reintentos

    raise RuntimeError(f"ADRES falló tras 2 intentos. Último error: {ultimo_error}")
