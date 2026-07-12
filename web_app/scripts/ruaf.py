# -*- coding: utf-8 -*-
import asyncio
import base64
import os
import re

import capsolver
import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

from ._captcha import resolver_imagen_2captcha

URL_TERMINOS = "https://ruaf.sispro.gov.co/TerminosCondiciones.aspx"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _resolver_captcha_imagen(api_key: str, imagen_base64: str) -> str:
    """Resuelve captcha de imagen: CapSolver OCR → 2captcha fallback. Síncrono, en thread."""
    capsolver.api_key = api_key
    for _ in range(3):
        try:
            solution = capsolver.solve({
                "type": "ImageToTextTask",
                "body": imagen_base64,
            })
            texto = solution.get("text", "").strip()
            if texto:
                return texto
        except Exception:
            pass

    # Fallback 2captcha (solo si TWOCAPTCHA_API_KEY está configurada)
    try:
        return resolver_imagen_2captcha(imagen_base64)
    except Exception as e:
        raise RuntimeError(f"CapSolver y 2captcha fallaron el captcha de imagen de RUAF: {e}")


def _resolver_captcha_landigai(parse_url: str, api_key: str, imagen_bytes: bytes) -> str:
    """Resuelve captcha con LandingAI en 2 pasos: Parse (imagen) -> Extract (schema)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    extract_url = parse_url.replace("/v1/ade/parse", "/v1/ade/extract")
    schema = (
        '{"type":"object","properties":{"captcha_text":{"type":"string"}},'
        '"required":["captcha_text"]}'
    )

    ultimo_error = "sin respuesta válida de LandingAI"
    try:
        with httpx.Client(timeout=45.0) as client:
            parse_files = {
                "document": ("captcha.png", imagen_bytes, "image/png"),
            }
            parse_data = {
                "model": "dpt-2-latest",
            }
            parse_resp = client.post(parse_url, headers=headers, files=parse_files, data=parse_data)
            if parse_resp.status_code >= 400:
                raise RuntimeError(f"parse HTTP {parse_resp.status_code}: {parse_resp.text[:300]}")
            parse_body = parse_resp.json()
            markdown = str((parse_body or {}).get("markdown") or "").strip()
            if not markdown:
                raise RuntimeError(f"parse sin markdown: {str(parse_body)[:300]}")

            extract_files = {
                "markdown": (None, markdown),
            }
            extract_data = {
                "schema": schema,
                "model": "extract-latest",
                "strict": "false",
            }
            extract_resp = client.post(
                extract_url,
                headers=headers,
                files=extract_files,
                data=extract_data,
            )
        if extract_resp.status_code >= 400:
            raise RuntimeError(f"extract HTTP {extract_resp.status_code}: {extract_resp.text[:300]}")

        body = extract_resp.json()
        extraction = body.get("extraction", {}) if isinstance(body, dict) else {}
        candidates = [
            extraction.get("captcha_text"),
            extraction.get("captcha"),
            extraction.get("text"),
        ]
        for c in candidates:
            txt = str(c or "").strip()
            if txt:
                return txt
        ultimo_error = f"extract sin campo usable: {str(body)[:300]}"
    except Exception as e:
        ultimo_error = str(e)

    raise RuntimeError(f"LandingAI no pudo resolver captcha RUAF: {ultimo_error}")


def _normalizar_captcha(texto: str) -> str:
    """Normaliza OCR para RUAF: solo alfanumérico, mayúsculas."""
    limpio = re.sub(r"[^A-Za-z0-9]", "", (texto or "").upper())
    # RUAF suele usar 4-6 caracteres; si llega más largo, recortar.
    if len(limpio) > 6:
        limpio = limpio[:6]
    return limpio


async def _intentar_descarga(cedula: str, dia: str, mes: str, anio: str,
                              output_dir: str, capsolver_api_key: str,
                              landigai_api_key: str | None = None,
                              landigai_api_url: str | None = None) -> str:
    print(f"[ruaf:{cedula}] inicio", flush=True)

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
        # Bloquear solo media — las imágenes son necesarias (captcha + logos del PDF)
        await context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type == "media"
            else route.continue_(),
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        try:
            # ── Paso 1: Términos y condiciones ──────────────────────────────
            # ponytail: 60s (no 30s) porque en Railway 6 Chromium en paralelo saturan CPU
            # y el goto de RUAF sufre contención; localmente carga en ~4s. Subir a per-job
            # semaphore si la contención persiste.
            await page.goto(URL_TERMINOS, wait_until="domcontentloaded", timeout=60_000)
            print(f"[ruaf:{cedula}] post-goto-terminos", flush=True)

            # Radio "Acepto" — buscar por value, id o label con texto "cepto"
            radio_acepto = page.locator(
                "input[type='radio'][value*='cepto'], "
                "input[type='radio'][id*='cepto'], "
                "input[type='radio'][id*='Acepto'], "
                "input[type='radio'][value='1'], "
                "input[type='radio']:first-of-type"
            ).first
            await radio_acepto.wait_for(timeout=15_000)
            await radio_acepto.click()

            # Botón Enviar
            boton_enviar = page.locator(
                "input[type='submit'][value*='nviar'], "
                "input[type='button'][value*='nviar'], "
                "button:has-text('Enviar'), "
                "input[value='Enviar']"
            ).first
            await boton_enviar.click(timeout=10_000)
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            print(f"[ruaf:{cedula}] post-enviar-terminos", flush=True)

            # ── Paso 2: Formulario Filtro.aspx ──────────────────────────────
            # Tipo de documento: Cédula de Ciudadanía (value="5|CC")
            dropdown = page.locator("#ddlTiposDocumentos")
            await dropdown.wait_for(timeout=15_000)
            try:
                await dropdown.select_option(value="5|CC")
            except Exception:
                await dropdown.select_option(label="CEDULA DE CIUDADANIA")
            print(f"[ruaf:{cedula}] tipo-doc-seleccionado", flush=True)

            # Número de identificación
            campo_num = page.locator(
                "input[id*='Numero'], input[id*='numero'], "
                "input[id*='Identificacion'], input[id*='txtId'], "
                "input[id*='txtNum']"
            ).first
            await campo_num.wait_for(timeout=10_000)
            await campo_num.fill(cedula)

            # Fecha de expedición — el sitio tiene campos separados día/mes/año
            campo_dia = page.locator(
                "input[id*='Dia'], input[id*='dia'], input[id*='Day'], "
                "select[id*='Dia'], select[id*='dia']"
            ).first
            campo_mes = page.locator(
                "input[id*='Mes'], input[id*='mes'], input[id*='Month'], "
                "select[id*='Mes'], select[id*='mes']"
            ).first
            campo_anio = page.locator(
                "input[id*='Ano'], input[id*='anio'], input[id*='Year'], input[id*='Anio'], "
                "select[id*='Ano'], select[id*='Anio']"
            ).first

            if await campo_dia.count() > 0 and await campo_mes.count() > 0:
                # Campos separados
                tag_dia  = await campo_dia.evaluate("el => el.tagName.toLowerCase()")
                tag_mes  = await campo_mes.evaluate("el => el.tagName.toLowerCase()")
                tag_anio = await campo_anio.evaluate("el => el.tagName.toLowerCase()") if await campo_anio.count() > 0 else "input"

                if tag_dia == "select":
                    await campo_dia.select_option(value=dia)
                else:
                    await campo_dia.fill(dia)

                if tag_mes == "select":
                    await campo_mes.select_option(value=mes)
                else:
                    await campo_mes.fill(mes)

                if await campo_anio.count() > 0:
                    if tag_anio == "select":
                        await campo_anio.select_option(value=anio)
                    else:
                        await campo_anio.fill(anio)
            else:
                # Fallback: campo único DD/MM/YYYY
                campo_fecha = page.locator(
                    "#MainContent_datepicker, "
                    "input[id*='Fecha'], input[id*='fecha'], input[id*='Expedicion'], "
                    "input[id*='datepicker'], input[name*='datepicker']"
                ).first
                if await campo_fecha.count() > 0:
                    await campo_fecha.fill(f"{dia}/{mes}/{anio}")
                    # Cerrar datepicker flotante para que no bloquee clicks posteriores.
                    await campo_fecha.press("Tab")
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(150)
            print(f"[ruaf:{cedula}] formulario-llenado", flush=True)

            # ── Pasos 3 y 4: Captcha → Verificar → Consultar (hasta 4 intentos) ──
            consultar_ok = False
            for intento_captcha in range(1, 5):
                # Captura robusta del captcha:
                # 1) URL real de CaptchaImage.axd usando la misma sesión del navegador
                # 2) Fallback a screenshot del elemento si la descarga falla
                img_el = page.locator(
                    "div[style*='background-color:White'] img[src*='CaptchaImage.axd'], "
                    "img[src*='CaptchaImage.axd'], img[alt='Captcha'], "
                    "img[id*='Captcha'], img[id*='captcha'], "
                    "img[src*='Captcha'], img[src*='captcha']"
                ).first
                if await img_el.count() == 0:
                    img_el = page.locator("#MainContent_txtCaptcha").locator("xpath=ancestor::tr[1]//img").first

                await img_el.wait_for(state="visible", timeout=10_000)
                try:
                    await page.wait_for_function(
                        """(el) => !!el && el.complete && (el.naturalWidth || 0) > 0 && (el.naturalHeight || 0) > 0""",
                        arg=await img_el.element_handle(),
                        timeout=8_000,
                    )
                except Exception:
                    pass

                captcha_url = await img_el.evaluate(
                    """(el) => {
                        const src = (el.currentSrc || el.src || '').trim();
                        return src || null;
                    }"""
                )

                img_bytes = b""
                if captcha_url and "CaptchaImage.axd" in captcha_url:
                    try:
                        resp = await context.request.get(
                            captcha_url,
                            headers={
                                "Cache-Control": "no-cache",
                                "Pragma": "no-cache",
                            },
                            timeout=15_000,
                        )
                        if resp.ok:
                            ctype = (resp.headers.get("content-type") or "").lower()
                            body = await resp.body()
                            if ("image" in ctype) and len(body) > 500:
                                img_bytes = body
                    except Exception:
                        img_bytes = b""

                if not img_bytes:
                    img_bytes = await img_el.screenshot()
                imagen_b64 = base64.b64encode(img_bytes).decode()

                # Guardar imagen de diagnóstico solo en el primer intento de cada ciclo externo
                if intento_captcha == 1:
                    debug_path = os.path.join(output_dir, f"ruaf_captcha_{cedula}.png")
                    with open(debug_path, "wb") as _f:
                        _f.write(img_bytes)

                # CapSolver falla este captcha devolviendo texto *incorrecto* (no vacío),
                # así que su fallback interno nunca dispara. Tras 2 intentos fallidos de
                # CapSolver, forzamos 2captcha directo en los intentos 3-4.
                if intento_captcha >= 3:
                    try:
                        texto_captcha = await asyncio.to_thread(resolver_imagen_2captcha, imagen_b64)
                        print(f"[ruaf:{cedula}] usando 2captcha (intento {intento_captcha})", flush=True)
                    except Exception as e:
                        print(f"[ruaf:{cedula}] 2captcha falló ({e}), cae a CapSolver", flush=True)
                        texto_captcha = await asyncio.to_thread(
                            _resolver_captcha_imagen, capsolver_api_key, imagen_b64
                        )
                elif landigai_api_key and landigai_api_url:
                    try:
                        texto_captcha = await asyncio.to_thread(
                            _resolver_captcha_landigai,
                            landigai_api_url,
                            landigai_api_key,
                            img_bytes,
                        )
                    except Exception as e:
                        print(f"[ruaf:{cedula}] LandigAI fallback a CapSolver: {e}", flush=True)
                        texto_captcha = await asyncio.to_thread(
                            _resolver_captcha_imagen, capsolver_api_key, imagen_b64
                        )
                else:
                    texto_captcha = await asyncio.to_thread(
                        _resolver_captcha_imagen, capsolver_api_key, imagen_b64
                    )
                texto_captcha = _normalizar_captcha(texto_captcha)
                print(f"[ruaf:{cedula}] captcha-texto={texto_captcha!r} (intento {intento_captcha})", flush=True)

                if not (4 <= len(texto_captcha) <= 6):
                    print(
                        f"[ruaf:{cedula}] captcha inválido por longitud ({len(texto_captcha)}), refrescando...",
                        flush=True,
                    )
                    try:
                        await page.locator("#MainContent_txtCaptcha").fill("")
                        await img_el.click(timeout=3_000)
                        await page.wait_for_timeout(600)
                    except Exception:
                        pass
                    continue

                await page.locator("#MainContent_txtCaptcha").fill(texto_captcha)

                # Click Verificar — PostBack ASP.NET
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(100)
                    await page.locator("#MainContent_btnVerify").click(timeout=10_000)
                except Exception:
                    # Fallback robusto para overlays (datepicker/elementos flotantes)
                    await page.evaluate(
                        """() => {
                            const btn = document.querySelector('#MainContent_btnVerify');
                            if (btn) btn.click();
                        }"""
                    )
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)

                # Indicador de captcha correcto: btnConsultar queda habilitado
                if await page.locator("#MainContent_btnConsultar").is_enabled():
                    print(f"[ruaf:{cedula}] captcha correcto — ejecutando Consultar", flush=True)
                    await page.locator("#MainContent_btnConsultar").click(timeout=10_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    print(f"[ruaf:{cedula}] post-consultar", flush=True)
                    consultar_ok = True
                    break

                print(f"[ruaf:{cedula}] captcha incorrecto (intento {intento_captcha}), reintentando...", flush=True)
                try:
                    await page.locator("#MainContent_txtCaptcha").fill("")
                    await img_el.click(timeout=3_000)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass

            if not consultar_ok:
                raise RuntimeError("RUAF: captcha incorrecto tras 4 intentos")

            # ── Paso 5: Descargar resultado ──────────────────────────────────
            ruta_pdf = os.path.join(output_dir, f"ruaf_{cedula}.pdf")
            ruta_png = os.path.join(output_dir, f"ruaf_{cedula}.png")

            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
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
                    raise RuntimeError(f"PDF de RUAF sospechosamente pequeño ({tamano} bytes)")
                print(f"[ruaf:{cedula}] post-PDF ({tamano} bytes)", flush=True)
                return ruta_pdf
            except RuntimeError:
                raise
            except Exception:
                await page.screenshot(path=ruta_png, full_page=True)
                tamano = os.path.getsize(ruta_png)
                if tamano < 10_000:
                    os.remove(ruta_png)
                    raise RuntimeError(f"PNG de RUAF sospechosamente pequeño ({tamano} bytes)")
                print(f"[ruaf:{cedula}] post-PNG ({tamano} bytes)", flush=True)
                return ruta_png

        except Exception as e:
            print(f"[ruaf:{cedula}] ERROR: {e}", flush=True)
            raise
        finally:
            await browser.close()


async def descargar(cedula: str, dia: str, mes: str, anio: str,
                    output_dir: str, capsolver_api_key: str,
                    landigai_api_key: str | None = None,
                    landigai_api_url: str | None = None) -> str:
    """Retorna ruta del archivo generado."""
    os.makedirs(output_dir, exist_ok=True)
    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 4):  # 3 intentos
        try:
            return await _intentar_descarga(
                cedula,
                dia,
                mes,
                anio,
                output_dir,
                capsolver_api_key,
                landigai_api_key,
                landigai_api_url,
            )
        except Exception as e:
            ultimo_error = e
            print(f"[ruaf:{cedula}] intento {intento} falló: {e}", flush=True)
            if intento < 3:
                await asyncio.sleep(3)
    raise RuntimeError(f"RUAF falló tras 3 intentos. Último error: {ultimo_error}")
