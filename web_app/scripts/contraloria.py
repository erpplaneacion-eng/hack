# -*- coding: utf-8 -*-
import asyncio
import os

import capsolver
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_FORMULARIO = "https://www.contraloria.gov.co/web/guest/persona-natural"
URL_CAPTCHA    = "https://cfiscal.contraloria.gov.co/Certificados/CertificadoPersonaNatural.aspx"
SITEKEY        = "6LcfnjwUAAAAAIyl8ehhox7ZYqLQSVl_w1dmYIle"


async def descargar(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    """Retorna ruta del archivo generado. Lanza excepción si falla."""
    os.makedirs(output_dir, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
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
        await asyncio.sleep(3)

        # Esperar hasta 30s a que el iframe de cfiscal cargue
        frame = None
        for _ in range(10):
            frame = page.frame(url="*cfiscal.contraloria.gov.co*")
            if not frame:
                frame = next((f for f in page.frames if "cfiscal" in f.url), None)
            if frame:
                break
            await asyncio.sleep(3)
        if not frame:
            raise RuntimeError("No se encontró el iframe de Contraloría")

        await frame.locator("#ddlTipoDocumento").select_option(label="Cédula de Ciudadanía")
        await frame.locator("#txtNumeroDocumento").fill(cedula)

        def _resolver():
            capsolver.api_key = capsolver_api_key
            token = ""
            for tipo in ["ReCaptchaV2TaskProxyless", "ReCaptchaV2EnterpriseTaskProxyless"]:
                try:
                    solution = capsolver.solve({
                        "type": tipo,
                        "websiteURL": URL_CAPTCHA,
                        "websiteKey": SITEKEY,
                    })
                    token = solution.get("gRecaptchaResponse", "")
                    if token:
                        break
                except Exception:
                    continue
            if not token:
                raise RuntimeError("CapSolver no pudo resolver el captcha de Contraloría")
            return token

        token = await asyncio.to_thread(_resolver)

        await frame.evaluate("""
            (token) => {
                const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (ta) { ta.value = token; ta.innerHTML = token; }
                if (window.Page_Validators) {
                    window.Page_Validators.forEach(v => { v.isvalid = true; });
                }
                window.Page_IsValid = true;
                window.WebForm_OnSubmit = () => true;
            }
        """, token)
        await asyncio.sleep(1)

        ruta_pdf = os.path.join(output_dir, f"contraloria_{cedula}.pdf")
        ruta_png = os.path.join(output_dir, f"contraloria_{cedula}.png")

        try:
            async with page.expect_download(timeout=20_000) as dl_info:
                await frame.locator("#btnBuscar").click()
            descarga = await dl_info.value
            nombre = descarga.suggested_filename or f"contraloria_{cedula}.pdf"
            ruta_final = os.path.join(output_dir, nombre)
            await descarga.save_as(ruta_final)
            await browser.close()
            return ruta_final

        except PlaywrightTimeout:
            await page.wait_for_load_state("load", timeout=20_000)
            await asyncio.sleep(3)
            try:
                await page.pdf(path=ruta_pdf, format="A4", print_background=True,
                               margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"})
                await browser.close()
                return ruta_pdf
            except Exception:
                await page.screenshot(path=ruta_png, full_page=True)
                await browser.close()
                return ruta_png
