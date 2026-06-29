# -*- coding: utf-8 -*-
import asyncio
import os
import re

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

from ._captcha import resolver_recaptcha

URL_INICIO     = "https://antecedentes.policia.gov.co:7005/WebJudicial/index.xhtml"
URL_FORMULARIO = "https://antecedentes.policia.gov.co:7005/WebJudicial/antecedentes.xhtml"
SITEKEY        = "6LcsIwQaAAAAAFCsaI-dkR6hgKsZwwJRsmE0tIJH"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)



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
            if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
                const findCallbacks = (obj, depth) => {
                    if (depth > 6 || !obj || typeof obj !== 'object') return;
                    if (typeof obj.callback === 'function') { obj.callback(token); return; }
                    Object.values(obj).forEach(v => findCallbacks(v, depth + 1));
                };
                Object.values(___grecaptcha_cfg.clients).forEach(c => findCallbacks(c, 0));
            }
        }
    """, token)


async def _intentar_descarga(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    print(f"[antecedentes:{cedula}] inicio", flush=True)

    # CapSolver → 2captcha en paralelo con la carga del browser; timeout 120s cubre el fallback
    captcha_task = asyncio.create_task(
        asyncio.wait_for(
            asyncio.to_thread(resolver_recaptcha, SITEKEY, URL_FORMULARIO, capsolver_api_key),
            timeout=120,
        )
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
            # Timeout 60s para tolerar latencia entre Railway y servidor :7005 colombiano
            await page.goto(URL_INICIO, wait_until="domcontentloaded", timeout=60_000)
            print(f"[antecedentes:{cedula}] post-goto", flush=True)

            # Aceptar términos y continuar
            await page.locator("#aceptaOption\\:0").click(timeout=20_000)
            await page.locator("#continuarBtn").click(timeout=20_000)
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)

            # Llenar cédula
            campo = page.locator("input[type='text'], input[type='number']").first
            await campo.wait_for(timeout=20_000)
            await campo.fill(cedula)

            # Esperar el token de CapSolver (probablemente ya está listo)
            try:
                token = await captcha_task
            except asyncio.TimeoutError:
                raise RuntimeError("CAPTCHA timeout >120s (CapSolver + 2captcha)")
            print(f"[antecedentes:{cedula}] post-captcha-task", flush=True)
            # Esperar que el widget reCAPTCHA haya registrado sus callbacks antes de inyectar
            try:
                await page.wait_for_function(
                    "() => typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients && Object.keys(___grecaptcha_cfg.clients).length > 0",
                    timeout=15_000,
                )
            except PlaywrightTimeout:
                pass  # si no aparece, intentar inyectar de todas formas
            await _inyectar_token(page, token)

            # Submit
            try:
                boton = page.locator(
                    "button:has-text('Consultar'), "
                    "input[value*='Consultar'], "
                    "button:has-text('Verificar'), "
                    "button[type='submit']"
                ).first
                await boton.click(timeout=15_000)
            except PlaywrightTimeout:
                await page.evaluate("document.querySelector('form').submit()")

            await page.wait_for_load_state("domcontentloaded", timeout=40_000)
            print(f"[antecedentes:{cedula}] post-submit", flush=True)

            # Validación post-submit no silenciosa: detectar éxito o error explícito
            await page.wait_for_function(
                """() => {
                    const t = document.body.innerText.toLowerCase();
                    const huboError = ['captcha', 'no es válido', 'intente nuevamente', 'error'].some(e => t.includes(e));
                    const exito = t.includes('antecedentes') && t.length > 200;
                    return exito || huboError;
                }""",
                timeout=30_000,
            )
            texto = (await page.inner_text("body")).lower()
            if any(e in texto for e in ["captcha inválido", "captcha no es válido", "intente nuevamente"]):
                raise RuntimeError("Antecedentes: servidor reportó captcha inválido")
            if "antecedentes" not in texto:
                raise RuntimeError("Antecedentes: página de resultado no apareció")
            print(f"[antecedentes:{cedula}] post-validacion-resultado", flush=True)

            # Esperar que terminen recursos (logos, fuentes) antes del PDF
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeout:
                pass

            ruta_pdf = os.path.join(output_dir, f"antecedentes_{cedula}.pdf")
            ruta_png = os.path.join(output_dir, f"antecedentes_{cedula}.png")
            print(f"[antecedentes:{cedula}] pre-PDF", flush=True)

            try:
                await page.pdf(
                    path=ruta_pdf, format="A4", print_background=True,
                    margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
                )
                tamano = os.path.getsize(ruta_pdf)
                if tamano < 10_000:
                    os.remove(ruta_pdf)
                    raise RuntimeError(f"PDF de antecedentes sospechosamente pequeño ({tamano} bytes)")
                print(f"[antecedentes:{cedula}] post-PDF ({tamano} bytes)", flush=True)
                return ruta_pdf
            except RuntimeError:
                raise
            except Exception:
                await page.screenshot(path=ruta_png, full_page=True)
                tamano = os.path.getsize(ruta_png)
                if tamano < 10_000:
                    os.remove(ruta_png)
                    raise RuntimeError(f"PNG de antecedentes sospechosamente pequeño ({tamano} bytes)")
                print(f"[antecedentes:{cedula}] post-PNG ({tamano} bytes)", flush=True)
                return ruta_png
        except Exception as e:
            print(f"[antecedentes:{cedula}] ERROR: {e}", flush=True)
            try:
                debug_path = os.path.join(output_dir, f"antecedentes_{cedula}_error_debug.png")
                await page.screenshot(path=debug_path, full_page=True)
                print(f"[antecedentes:{cedula}] screenshot de error: {debug_path}", flush=True)
            except Exception:
                pass
            raise
        finally:
            # Si el browser se cierra antes que el captcha, cancelar la tarea
            if not captcha_task.done():
                captcha_task.cancel()
            await browser.close()


async def descargar(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    """Retorna ruta del archivo generado."""
    os.makedirs(output_dir, exist_ok=True)
    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 4):  # 3 intentos
        try:
            return await _intentar_descarga(cedula, output_dir, capsolver_api_key)
        except Exception as e:
            ultimo_error = e
            print(f"[antecedentes:{cedula}] intento {intento} falló: {e}", flush=True)
            if intento < 3:
                msg = str(e).lower()
                if "captcha inválido" in msg or "captcha no es válido" in msg:
                    await asyncio.sleep(8)
                elif "timeout" in msg:
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(3)
    raise RuntimeError(f"Antecedentes falló tras 3 intentos. Último error: {ultimo_error}")
