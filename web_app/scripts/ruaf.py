# -*- coding: utf-8 -*-
import asyncio
import base64
import os

import capsolver
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

URL_TERMINOS = "https://ruaf.sispro.gov.co/TerminosCondiciones.aspx"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _resolver_captcha_imagen(api_key: str, imagen_base64: str) -> str:
    """Resuelve captcha de imagen con CapSolver OCR — síncrono, se ejecuta en thread."""
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
    raise RuntimeError("CapSolver no pudo resolver el captcha de imagen de RUAF")


async def _intentar_descarga(cedula: str, dia: str, mes: str, anio: str,
                              output_dir: str, capsolver_api_key: str) -> str:
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
            await page.goto(URL_TERMINOS, wait_until="domcontentloaded", timeout=30_000)
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
                    "input[id*='Fecha'], input[id*='fecha'], input[id*='Expedicion']"
                ).first
                if await campo_fecha.count() > 0:
                    await campo_fecha.fill(f"{dia}/{mes}/{anio}")
            print(f"[ruaf:{cedula}] formulario-llenado", flush=True)

            # ── Pasos 3 y 4: Captcha → Verificar → Consultar (hasta 4 intentos) ──
            consultar_ok = False
            for intento_captcha in range(1, 5):
                # Obtener imagen sin caché del navegador:
                # fetch con cache:'no-store' garantiza que cada intento ve la imagen actual del servidor
                imagen_b64 = await page.evaluate("""
                    async () => {
                        // Buscar por id/src; fallback: img más cercano al input del captcha
                        let img = document.querySelector(
                            'img[id*="Captcha"], img[id*="captcha"], ' +
                            'img[src*="Captcha"], img[src*="captcha"], ' +
                            'img[src*="Image"], img[src*="image"]'
                        );
                        if (!img) {
                            const inp = document.getElementById('MainContent_txtCaptcha');
                            if (inp) {
                                let el = inp.parentElement;
                                for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                                    const found = el.querySelector('img');
                                    if (found) { img = found; break; }
                                }
                            }
                        }
                        if (!img || !img.src || img.src.startsWith('data:')) {
                            // Imagen embebida (data URI) — retornar src directamente
                            if (img && img.src.startsWith('data:')) {
                                return img.src.split(',')[1];
                            }
                            return null;
                        }
                        try {
                            const r = await fetch(img.src, {
                                cache: 'no-store',
                                headers: {'Cache-Control': 'no-cache', 'Pragma': 'no-cache'}
                            });
                            const buf = await r.arrayBuffer();
                            let bin = '';
                            new Uint8Array(buf).forEach(b => bin += String.fromCharCode(b));
                            return btoa(bin);
                        } catch(e) { return null; }
                    }
                """)

                if not imagen_b64:
                    # Último recurso: screenshot del elemento
                    img_el = page.locator(
                        "img[id*='Captcha'], img[id*='captcha'], "
                        "img[src*='Captcha'], img[src*='captcha']"
                    ).first
                    if await img_el.count() == 0:
                        img_el = page.locator("#MainContent_txtCaptcha").locator("xpath=../img")
                    img_bytes = await img_el.screenshot()
                    imagen_b64 = base64.b64encode(img_bytes).decode()

                # Guardar imagen de diagnóstico solo en el primer intento de cada ciclo externo
                if intento_captcha == 1:
                    debug_path = os.path.join(output_dir, f"ruaf_captcha_{cedula}.png")
                    with open(debug_path, "wb") as _f:
                        _f.write(base64.b64decode(imagen_b64))

                texto_captcha = await asyncio.to_thread(
                    _resolver_captcha_imagen, capsolver_api_key, imagen_b64
                )
                print(f"[ruaf:{cedula}] captcha-texto={texto_captcha!r} (intento {intento_captcha})", flush=True)

                await page.locator("#MainContent_txtCaptcha").fill(texto_captcha)

                # Click Verificar — PostBack ASP.NET
                await page.locator("#MainContent_btnVerify").click(timeout=10_000)
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
                    output_dir: str, capsolver_api_key: str) -> str:
    """Retorna ruta del archivo generado."""
    os.makedirs(output_dir, exist_ok=True)
    ultimo_error: Exception = RuntimeError("Sin intentos realizados")
    for intento in range(1, 4):  # 3 intentos
        try:
            return await _intentar_descarga(cedula, dia, mes, anio, output_dir, capsolver_api_key)
        except Exception as e:
            ultimo_error = e
            print(f"[ruaf:{cedula}] intento {intento} falló: {e}", flush=True)
            if intento < 3:
                await asyncio.sleep(3)
    raise RuntimeError(f"RUAF falló tras 3 intentos. Último error: {ultimo_error}")
