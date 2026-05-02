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
    for tipo in ["ReCaptchaV2EnterpriseTaskProxyless", "ReCaptchaV3TaskProxyless", "ReCaptchaV2TaskProxyless"]:
        for _intento in range(3):
            try:
                payload = {"type": tipo, "websiteURL": URL_CAPTCHA, "websiteKey": SITEKEY}
                if tipo == "ReCaptchaV3TaskProxyless":
                    payload["pageAction"] = "submit"
                solution = capsolver.solve(payload)
                token = solution.get("gRecaptchaResponse", "")
                if token:
                    return token
                break  # token vacío → siguiente tipo
            except Exception:
                if _intento == 2:
                    break  # agotó 3 intentos → siguiente tipo
    raise RuntimeError("CapSolver no pudo resolver el captcha de Contraloría")


async def _intentar_descarga(cedula: str, output_dir: str, capsolver_api_key: str) -> str:
    print(f"[contraloria:{cedula}] inicio", flush=True)

    # CapSolver en paralelo desde el inicio; timeout 90s para no agotar el budget global
    captcha_task = asyncio.create_task(
        asyncio.wait_for(
            asyncio.to_thread(_resolver_captcha, capsolver_api_key),
            timeout=90,
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
            # domcontentloaded en lugar de "load" — los trackers .gov.co cuelgan el evento load
            await page.goto(URL_FORMULARIO, wait_until="domcontentloaded", timeout=60_000)
            print(f"[contraloria:{cedula}] post-goto", flush=True)

            # Buscar iframe cfiscal: esperar el elemento interno directamente via FrameLocator
            # garantiza que page.frames ya tiene el frame registrado cuando terminamos
            await page.wait_for_selector("iframe[src*='cfiscal']", timeout=45_000)
            frame_loc = page.frame_locator("iframe[src*='cfiscal']")
            await frame_loc.locator("#ddlTipoDocumento").wait_for(timeout=15_000)
            frame = next((f for f in page.frames if "cfiscal" in (f.url or "")), None)
            if not frame:
                raise RuntimeError("iframe cfiscal: el Frame nunca se registró en page.frames")
            print(f"[contraloria:{cedula}] frame-encontrado", flush=True)

            await frame.locator("#ddlTipoDocumento").select_option(label="Cédula de Ciudadanía")
            await frame.locator("#txtNumeroDocumento").fill(cedula)

            try:
                token = await captcha_task
            except asyncio.TimeoutError:
                raise RuntimeError("CapSolver timeout >90s")
            print(f"[contraloria:{cedula}] post-captcha", flush=True)

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
                async with page.expect_download(timeout=60_000) as dl_info:
                    await frame.locator("#btnBuscar").click()
                descarga = await dl_info.value
                nombre = descarga.suggested_filename or f"contraloria_{cedula}.pdf"
                ruta_final = os.path.join(output_dir, nombre)
                await descarga.save_as(ruta_final)
                tamano = os.path.getsize(ruta_final)
                if tamano < 10_000:
                    os.remove(ruta_final)
                    raise RuntimeError(f"Contraloría: archivo descargado sospechosamente pequeño ({tamano} bytes)")
                print(f"[contraloria:{cedula}] descarga-directa ({tamano} bytes)", flush=True)
                return ruta_final

            except PlaywrightTimeout:
                # El servidor renderizó el certificado en la misma página — generar PDF
                print(f"[contraloria:{cedula}] sin descarga directa, fallback a page.pdf", flush=True)

                # Validar que no es una página de error antes de intentar PDF
                try:
                    contenido = (await frame.inner_text("body")).lower()
                except Exception:
                    contenido = ""
                if any(e in contenido for e in ["captcha", "no es válido", "intente nuevamente"]):
                    raise RuntimeError("Contraloría: frame devolvió error tras submit")

                try:
                    await page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightTimeout:
                    pass
                try:
                    await page.pdf(
                        path=ruta_pdf, format="A4", print_background=True,
                        margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"},
                    )
                    tamano = os.path.getsize(ruta_pdf)
                    if tamano < 10_000:
                        os.remove(ruta_pdf)
                        raise RuntimeError(f"PDF de contraloría sospechosamente pequeño ({tamano} bytes)")
                    print(f"[contraloria:{cedula}] post-PDF ({tamano} bytes)", flush=True)
                    return ruta_pdf
                except RuntimeError:
                    raise
                except Exception:
                    await page.screenshot(path=ruta_png, full_page=True)
                    tamano = os.path.getsize(ruta_png)
                    if tamano < 10_000:
                        os.remove(ruta_png)
                        raise RuntimeError(f"PNG de contraloría sospechosamente pequeño ({tamano} bytes)")
                    print(f"[contraloria:{cedula}] post-PNG ({tamano} bytes)", flush=True)
                    return ruta_png
        except Exception as e:
            print(f"[contraloria:{cedula}] ERROR: {e}", flush=True)
            try:
                debug_path = os.path.join(output_dir, f"contraloria_{cedula}_error_debug.png")
                await page.screenshot(path=debug_path, full_page=True)
                print(f"[contraloria:{cedula}] screenshot de error: {debug_path}", flush=True)
            except Exception:
                pass
            raise
        finally:
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
            print(f"[contraloria:{cedula}] intento {intento} falló: {e}", flush=True)
            if intento < 3:
                msg = str(e).lower()
                if "captcha inválido" in msg or "captcha no es válido" in msg:
                    await asyncio.sleep(8)
                elif "timeout" in msg:
                    await asyncio.sleep(10)
                else:
                    await asyncio.sleep(3)
    raise RuntimeError(f"Contraloría falló tras 3 intentos. Último error: {ultimo_error}")
