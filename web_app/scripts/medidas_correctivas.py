# -*- coding: utf-8 -*-
import asyncio
import os

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_FORMULARIO = "https://srvcnpc.policia.gov.co/PSC/frm_cnp_consulta.aspx"


async def descargar(cedula: str, dia: str, mes: str, anio: str, output_dir: str) -> str:
    """Retorna ruta del archivo generado. Lanza excepción si falla."""
    os.makedirs(output_dir, exist_ok=True)
    fecha = f"{dia}/{mes}/{anio}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
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

        await page.goto(URL_FORMULARIO, wait_until="networkidle", timeout=60_000)
        await asyncio.sleep(2)

        # 1. Seleccionar Cédula de Ciudadanía — dispara AJAX que muestra fecha y btnConsultar2
        select_tipo = page.locator("#ctl00_ContentPlaceHolder3_ddlTipoDoc")
        await select_tipo.wait_for(timeout=10_000)
        await select_tipo.select_option(value="55")
        await page.wait_for_load_state("networkidle", timeout=20_000)
        await asyncio.sleep(2)

        # 2. Ingresar cédula
        campo_doc = page.locator("#ctl00_ContentPlaceHolder3_txtExpediente")
        await campo_doc.wait_for(timeout=10_000)
        await campo_doc.fill(cedula)
        await asyncio.sleep(0.3)

        # 3. Ingresar fecha — esperar que el campo aparezca (confirma que el AJAX completó)
        campo_fecha = page.locator("#txtFechaexp")
        try:
            await campo_fecha.wait_for(state="visible", timeout=10_000)
            await campo_fecha.click()
            await campo_fecha.fill("")
            await campo_fecha.type(fecha, delay=80)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            if await campo_fecha.input_value() != fecha:
                await campo_fecha.fill(fecha)
                await page.keyboard.press("Tab")
                await asyncio.sleep(0.3)
            # La fecha puede disparar otro postback menor
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightTimeout:
            pass  # Algunos tipos de doc no requieren fecha

        await asyncio.sleep(1)

        # 4. Esperar que el loader desaparezca y la página esté lista
        loader = page.locator(".loader_decad")
        try:
            await loader.wait_for(state="hidden", timeout=20_000)
        except PlaywrightTimeout:
            pass
        await page.wait_for_load_state("networkidle", timeout=15_000)
        await asyncio.sleep(1)

        # 5. Verificar que __doPostBack está disponible (confirma que el UpdatePanel cargó)
        postback_ok = await page.evaluate("typeof __doPostBack === 'function'")
        if not postback_ok:
            ruta_diag = os.path.join(output_dir, "rnmc_debug.png")
            await page.screenshot(path=ruta_diag, full_page=True)
            raise RuntimeError("__doPostBack no disponible en RNMC — el AJAX no completó. Ver rnmc_debug.png")

        ruta_pdf = os.path.join(output_dir, f"medidas_correctivas_{cedula}.pdf")
        ruta_png = os.path.join(output_dir, f"medidas_correctivas_{cedula}.png")

        JS_CLICK = (
            "if(document.querySelector('#ctl00_ContentPlaceHolder3_btnConsultar2')){"
            "  __doPostBack('ctl00$ContentPlaceHolder3$btnConsultar2','');"
            "} else {"
            "  __doPostBack('ctl00$ContentPlaceHolder3$btnConsultar','');"
            "}"
        )

        # Disparar el postback una sola vez y esperar resultado
        try:
            async with page.expect_download(timeout=15_000) as dl_info:
                await page.evaluate(JS_CLICK)
            descarga = await dl_info.value
            nombre = descarga.suggested_filename or f"medidas_correctivas_{cedula}.pdf"
            ruta_final = os.path.join(output_dir, nombre)
            await descarga.save_as(ruta_final)
            await browser.close()
            return ruta_final
        except Exception:
            pass  # Sin descarga directa — el resultado se muestra en página

        # Esperar que cargue el resultado en la misma página
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(3)

        # Capturar resultado: PDF si headless, PNG si headed
        try:
            await page.pdf(
                path=ruta_pdf, format="A4", print_background=True,
                margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
            )
            await browser.close()
            return ruta_pdf
        except Exception:
            await page.screenshot(path=ruta_png, full_page=True)
            await browser.close()
            return ruta_png
