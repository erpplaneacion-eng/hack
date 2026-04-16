# -*- coding: utf-8 -*-
import asyncio
import os
import re

import capsolver
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_INICIO     = "https://antecedentes.policia.gov.co:7005/WebJudicial/index.xhtml"
URL_FORMULARIO = "https://antecedentes.policia.gov.co:7005/WebJudicial/antecedentes.xhtml"
SITEKEY_DEFAULT = "6LcsIwQaAAAAAFCsaI-dkR6hgKsZwwJRsmE0tIJH"


async def _extraer_sitekey(page) -> str:
    sitekey = await page.evaluate("""
        () => {
            const el = document.querySelector('[data-sitekey]');
            return el ? el.getAttribute('data-sitekey') : null;
        }
    """)
    if sitekey:
        return sitekey

    content = await page.content()
    match = re.search(r'recaptcha.*?[?&]k=([A-Za-z0-9_-]{30,})', content)
    if match:
        return match.group(1)

    match = re.search(r'"sitekey"\s*:\s*"([^"]+)"', content)
    if match:
        return match.group(1)

    match = re.search(r'sitekey[=:]\s*[\'"]([0-9A-Za-z_-]{20,})[\'"]', content)
    if match:
        return match.group(1)

    return SITEKEY_DEFAULT


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

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        await page.goto(URL_INICIO, wait_until="load", timeout=60_000)
        await asyncio.sleep(1)

        await page.locator("#aceptaOption\\:0").click(timeout=10_000)
        await asyncio.sleep(0.5)
        await page.locator("#continuarBtn").click(timeout=10_000)
        await page.wait_for_load_state("load", timeout=20_000)
        await asyncio.sleep(1)

        campo = page.locator("input[type='text'], input[type='number']").first
        await campo.wait_for(timeout=10_000)
        await campo.fill(cedula)
        await asyncio.sleep(3)

        sitekey = await _extraer_sitekey(page)

        page_url = page.url

        def _resolver():
            capsolver.api_key = capsolver_api_key
            token = ""
            for tipo in [
                "ReCaptchaV3TaskProxyless",
                "ReCaptchaV2EnterpriseTaskProxyless",
                "ReCaptchaV2TaskProxyless",
            ]:
                try:
                    payload = {"type": tipo, "websiteURL": page_url, "websiteKey": sitekey}
                    if tipo == "ReCaptchaV3TaskProxyless":
                        payload["pageAction"] = "verify"
                    solution = capsolver.solve(payload)
                    token = solution.get("gRecaptchaResponse", "")
                    if token:
                        break
                except Exception:
                    continue
            if not token:
                raise RuntimeError("CapSolver no devolvió token para Policía antecedentes")
            return token

        token = await asyncio.to_thread(_resolver)

        await _inyectar_token(page, token)
        await asyncio.sleep(1)

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

        await page.wait_for_load_state("load", timeout=20_000)
        await asyncio.sleep(2)

        ruta_pdf = os.path.join(output_dir, f"antecedentes_{cedula}.pdf")
        ruta_png = os.path.join(output_dir, f"antecedentes_{cedula}.png")

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
