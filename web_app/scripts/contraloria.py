# -*- coding: utf-8 -*-
import asyncio
import os

import capsolver
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_FORMULARIO = "https://www.contraloria.gov.co/web/guest/persona-natural"
URL_CAPTCHA    = "https://cfiscal.contraloria.gov.co/Certificados/CertificadoPersonaNatural.aspx"
SITEKEY        = "6LcfnjwUAAAAAIyl8ehhox7ZYqLQSVl_w1dmYIle"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _resolver_captcha(api_key: str) -> str:
    capsolver.api_key = api_key
    for tipo in ["ReCaptchaV2TaskProxyless", "ReCaptchaV2EnterpriseTaskProxyless"]:
        try:
            solution = capsolver.solve({
                "type": tipo,
                "websiteURL": URL_CAPTCHA,
                "websiteKey": SITEKEY,
            })
            token = solution.get("gRecaptchaResponse", "")
            if token:
                return token
        except Exception:
            continue
    raise RuntimeError("CapSolver no pudo resolver el captcha de Contraloría")


async def _intentar_descarga(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    # CapSolver en paralelo desde el inicio
    captcha_task = asyncio.create_task(
        asyncio.to_thread(_resolver_captcha, capsolver_api_key)
    )

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
        # Bloquear sólo media — imágenes y fuentes son necesarias por si cae al fallback page.pdf()
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type == "media"
            else route.continue_(),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            # wait_until="load" — el iframe de cfiscal se inyecta por JS tras el load completo
            await page.goto(URL_FORMULARIO, wait_until="load", timeout=60_000)

            # Esperar a que el iframe de cfiscal aparezca — timeout extendido para producción
            try:
                await page.wait_for_function(
                    """() => Array.from(document.querySelectorAll('iframe'))
                        .some(f => f.src && f.src.includes('cfiscal'))""",
                    timeout=45_000,
                )
            except PlaywrightTimeout:
                raise RuntimeError("No apareció el iframe de Contraloría en 45s")

            # Esperar que el frame realmente cargue su contenido
            frame = None
            for _ in range(30):
                frame = next((f for f in page.frames if "cfiscal" in (f.url or "")), None)
                if frame:
                    try:
                        await frame.wait_for_selector("#ddlTipoDocumento", timeout=2_000)
                        break
                    except PlaywrightTimeout:
                        frame = None
                await asyncio.sleep(0.5)

            if not frame:
                raise RuntimeError("No se encontró el iframe de Contraloría")

            await frame.locator("#ddlTipoDocumento").select_option(label="Cédula de Ciudadanía")
            await frame.locator("#txtNumeroDocumento").fill(cedula)

            token = await captcha_task

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

            ruta_pdf = os.path.join(output_dir, f"contraloria_{cedula}.pdf")
            ruta_png = os.path.join(output_dir, f"contraloria_{cedula}.png")

            try:
                async with page.expect_download(timeout=30_000) as dl_info:
                    await frame.locator("#btnBuscar").click()
                descarga = await dl_info.value
                nombre = descarga.suggested_filename or f"contraloria_{cedula}.pdf"
                ruta_final = os.path.join(output_dir, nombre)
                await descarga.save_as(ruta_final)
                return ruta_final

            except PlaywrightTimeout:
                # El servidor renderizó el certificado en la misma página — generar PDF
                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
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
            if not captcha_task.done():
                captcha_task.cancel()
            await browser.close()


async def descargar(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    """Retorna ruta del archivo generado. Reintenta hasta 2 veces antes de lanzar."""
    os.makedirs(output_dir, exist_ok=True)

    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 3):
        try:
            return await _intentar_descarga(cedula, output_dir, capsolver_api_key)
        except Exception as e:
            ultimo_error = e
            if intento < 2:
                await asyncio.sleep(3)

    raise RuntimeError(f"Contraloría falló tras 2 intentos. Último error: {ultimo_error}")
