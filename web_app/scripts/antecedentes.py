# -*- coding: utf-8 -*-
import asyncio
import os
import re

import capsolver
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_INICIO     = "https://antecedentes.policia.gov.co:7005/WebJudicial/index.xhtml"
URL_FORMULARIO = "https://antecedentes.policia.gov.co:7005/WebJudicial/antecedentes.xhtml"
SITEKEY        = "6LcsIwQaAAAAAFCsaI-dkR6hgKsZwwJRsmE0tIJH"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _resolver_captcha(api_key: str, page_url: str = URL_FORMULARIO) -> str:
    """Llama a CapSolver — síncrono, se ejecuta en thread."""
    capsolver.api_key = api_key
    for tipo in [
        "ReCaptchaV3TaskProxyless",
        "ReCaptchaV2EnterpriseTaskProxyless",
        "ReCaptchaV2TaskProxyless",
    ]:
        try:
            payload = {"type": tipo, "websiteURL": page_url, "websiteKey": SITEKEY}
            if tipo == "ReCaptchaV3TaskProxyless":
                payload["pageAction"] = "verify"
            solution = capsolver.solve(payload)
            token = solution.get("gRecaptchaResponse", "")
            if token:
                return token
        except Exception:
            continue
    raise RuntimeError("CapSolver no devolvió token para Policía antecedentes")


async def _inyectar_token(page, token: str):
    await page.evaluate("""
        (token) => {
            const responseField = document.getElementById('g-recaptcha-response');
            if (responseField) {
                responseField.innerHTML = token;
                responseField.value = token;
            }
            document.querySelectorAll('textarea[name="g-recaptcha-response"]').forEach(el => {
                el.innerHTML = token;
                el.value = token;
            });
            if (typeof ___grecaptcha_cfg !== 'undefined') {
                Object.entries(___grecaptcha_cfg.clients).forEach(([key, client]) => {
                    const callback = client?.U?.l?.callback || client?.aa?.l?.callback;
                    if (typeof callback === 'function') callback(token);
                });
            }
        }
    """, token)


async def descargar(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    """Retorna ruta del archivo generado. Lanza excepción si falla."""
    os.makedirs(output_dir, exist_ok=True)

    # Lanzar CapSolver en paralelo con la carga de la página (la sitekey es constante)
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
            viewport={"width": 1280, "height": 800},
            user_agent=USER_AGENT,
        )
        # Bloquear recursos pesados — el PDF no los necesita
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            await page.goto(URL_INICIO, wait_until="domcontentloaded", timeout=30_000)

            # Aceptar términos y continuar
            await page.locator("#aceptaOption\\:0").click(timeout=10_000)
            await page.locator("#continuarBtn").click(timeout=10_000)
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)

            # Llenar cédula
            campo = page.locator("input[type='text'], input[type='number']").first
            await campo.wait_for(timeout=10_000)
            await campo.fill(cedula)

            # Esperar el token de CapSolver (probablemente ya está listo)
            token = await captcha_task
            await _inyectar_token(page, token)

            # Submit
            try:
                boton = page.locator(
                    "button:has-text('Consultar'), "
                    "input[value*='Consultar'], "
                    "button:has-text('Verificar'), "
                    "button[type='submit']"
                ).first
                await boton.click(timeout=8_000)
            except PlaywrightTimeout:
                await page.evaluate("document.querySelector('form').submit()")

            await page.wait_for_load_state("domcontentloaded", timeout=20_000)

            # Esperar que aparezca contenido del resultado (no un spinner ni la misma página)
            try:
                await page.wait_for_function(
                    """() => {
                        const t = document.body.innerText.toLowerCase();
                        return t.includes('antecedentes') && t.length > 200;
                    }""",
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                pass  # seguir y generar PDF de lo que haya

            ruta_pdf = os.path.join(output_dir, f"antecedentes_{cedula}.pdf")
            ruta_png = os.path.join(output_dir, f"antecedentes_{cedula}.png")

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
            # Si el browser se cierra antes que el captcha, cancelar la tarea
            if not captcha_task.done():
                captcha_task.cancel()
            await browser.close()
