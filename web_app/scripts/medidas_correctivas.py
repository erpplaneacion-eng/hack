# -*- coding: utf-8 -*-
import asyncio
import os

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_FORMULARIO = "https://srvcnpc.policia.gov.co/PSC/frm_cnp_consulta.aspx"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def descargar(cedula: str, dia: str, mes: str, anio: str, output_dir: str) -> str:
    """Retorna ruta del archivo generado. Lanza excepción si falla."""
    os.makedirs(output_dir, exist_ok=True)
    fecha = f"{dia}/{mes}/{anio}"

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
        # Bloquear sólo media — imágenes y fuentes son necesarias para el PDF (logos)
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type == "media"
            else route.continue_(),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            await page.goto(URL_FORMULARIO, wait_until="domcontentloaded", timeout=30_000)

            # 1. Seleccionar Cédula de Ciudadanía — dispara AJAX que muestra fecha y btnConsultar2
            await page.locator("#ctl00_ContentPlaceHolder3_ddlTipoDoc").wait_for(timeout=10_000)
            await page.locator("#ctl00_ContentPlaceHolder3_ddlTipoDoc").select_option(value="55")

            # Esperar a que el AJAX termine: el campo de fecha debe aparecer visible
            try:
                await page.wait_for_function(
                    """() => {
                        const f = document.querySelector('#txtFechaexp');
                        return f && f.offsetParent !== null;
                    }""",
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                pass  # algunos tipos no requieren fecha

            # 2. Ingresar cédula
            await page.locator("#ctl00_ContentPlaceHolder3_txtExpediente").fill(cedula)

            # 3. Ingresar fecha si el campo está disponible
            campo_fecha = page.locator("#txtFechaexp")
            if await campo_fecha.count() > 0 and await campo_fecha.is_visible():
                await campo_fecha.click()
                await campo_fecha.fill("")
                await campo_fecha.type(fecha, delay=50)
                await page.keyboard.press("Escape")
                if await campo_fecha.input_value() != fecha:
                    await campo_fecha.fill(fecha)
                    await page.keyboard.press("Tab")

            # 4. Esperar que el loader desaparezca y __doPostBack esté disponible
            try:
                await page.wait_for_function(
                    """() => {
                        const l = document.querySelector('.loader_decad');
                        const loaderHidden = !l || l.offsetParent === null;
                        return loaderHidden && typeof __doPostBack === 'function';
                    }""",
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                postback_ok = await page.evaluate("typeof __doPostBack === 'function'")
                if not postback_ok:
                    raise RuntimeError("__doPostBack no disponible en RNMC — el AJAX no completó")

            ruta_pdf = os.path.join(output_dir, f"medidas_correctivas_{cedula}.pdf")
            ruta_png = os.path.join(output_dir, f"medidas_correctivas_{cedula}.png")

            JS_CLICK = (
                "if(document.querySelector('#ctl00_ContentPlaceHolder3_btnConsultar2')){"
                "  __doPostBack('ctl00$ContentPlaceHolder3$btnConsultar2','');"
                "} else {"
                "  __doPostBack('ctl00$ContentPlaceHolder3$btnConsultar','');"
                "}"
            )

            # Disparar el postback y esperar descarga directa
            try:
                async with page.expect_download(timeout=15_000) as dl_info:
                    await page.evaluate(JS_CLICK)
                descarga = await dl_info.value
                nombre = descarga.suggested_filename or f"medidas_correctivas_{cedula}.pdf"
                ruta_final = os.path.join(output_dir, nombre)
                await descarga.save_as(ruta_final)
                return ruta_final
            except Exception:
                pass  # Sin descarga directa — el resultado se muestra en página

            # Esperar que el resultado aparezca en página
            try:
                await page.wait_for_function(
                    """() => {
                        const t = document.body.innerText.toLowerCase();
                        return t.length > 500 && (
                            t.includes('no registra') ||
                            t.includes('registra') ||
                            t.includes('medida') ||
                            t.includes('certificado')
                        );
                    }""",
                    timeout=20_000,
                )
            except PlaywrightTimeout:
                pass

            try:
                await page.pdf(
                    path=ruta_pdf, format="A4", print_background=True,
                    margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
                )
                return ruta_pdf
            except Exception:
                await page.screenshot(path=ruta_png, full_page=True)
                return ruta_png
        finally:
            await browser.close()
