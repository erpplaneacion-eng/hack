# -*- coding: utf-8 -*-
"""Resolución de reCAPTCHA Enterprise: CapSolver → 2captcha fallback.
Síncrono, diseñado para asyncio.to_thread."""
import os
import time

import capsolver
import httpx

_2CAPTCHA_SUBMIT = "https://2captcha.com/in.php"
_2CAPTCHA_RESULT = "https://2captcha.com/res.php"


ENTERPRISE = "ReCaptchaV2EnterpriseTaskProxyless"
V2 = "ReCaptchaV2TaskProxyless"


def resolver_recaptcha(sitekey: str, url: str, capsolver_key: str, tipo: str = ENTERPRISE,
                       presupuesto_s: float = 110) -> str:
    """Resuelve y retorna el token. `presupuesto_s` es un techo duro: el hilo termina
    solo antes de que el `asyncio.wait_for` del llamador lo abandone corriendo
    (to_thread no se puede cancelar — sin esto quedan hilos zombies ocupando el pool).

    `tipo` es propiedad de cada sitio, no algo a descubrir en runtime: Contraloría es
    Enterprise, Policía es V2 clásico. Cruzarlos produce un token que el servidor
    rechaza con "does not match the displayed text"."""
    t0 = time.monotonic()
    restante = lambda: presupuesto_s - (time.monotonic() - t0)

    # 1. CapSolver. ponytail: un solo tipo, el que corresponde al sitio. La cascada de
    # 3 tipos × 3 intentos se comía el presupuesto entero y el fallback a 2captcha nunca
    # llegaba a correr; además V3 devuelve "wrong captcha type" en ambas sitekeys.
    capsolver.api_key = capsolver_key
    payload = {"type": tipo, "websiteURL": url, "websiteKey": sitekey}
    for intento in (1, 2):
        if restante() < 40:  # no arrancar un solve de ~25s sin margen para el fallback
            break
        try:
            token = capsolver.solve(payload).get("gRecaptchaResponse", "")
            if token:
                return token
            print(f"[captcha] CapSolver devolvió token vacío (intento {intento})", flush=True)
        except Exception as e:
            print(f"[captcha] CapSolver falló (intento {intento}): {e}", flush=True)

    # 2. Fallback: 2captcha
    tc_key = os.getenv("TWOCAPTCHA_API_KEY", "")
    if not tc_key:
        raise RuntimeError("CapSolver agotado y TWOCAPTCHA_API_KEY no configurada")
    print(f"[captcha] cayendo a 2captcha ({restante():.0f}s de presupuesto)", flush=True)

    with httpx.Client(timeout=30) as client:
        r = client.post(_2CAPTCHA_SUBMIT, data={
            "key": tc_key, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": url,
            "enterprise": 1 if tipo == ENTERPRISE else 0, "json": 1,
        })
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 1:
            raise RuntimeError(f"2captcha submit error: {data.get('request')}")
        task_id = data["request"]

        while restante() > 6:
            time.sleep(5)
            r = client.get(_2CAPTCHA_RESULT, params={
                "key": tc_key, "action": "get", "id": task_id, "json": 1,
            })
            data = r.json()
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                raise RuntimeError(f"2captcha error: {data.get('request')}")

    raise RuntimeError(f"2captcha: presupuesto de {presupuesto_s:.0f}s agotado")


def resolver_imagen_2captcha(imagen_base64: str, min_len: int = 4, max_len: int = 6) -> str:
    """OCR de captcha de imagen vía 2captcha (método base64). Lee TWOCAPTCHA_API_KEY
    del entorno. Síncrono, para asyncio.to_thread. Lanza RuntimeError si falla."""
    tc_key = os.getenv("TWOCAPTCHA_API_KEY", "")
    if not tc_key:
        raise RuntimeError("TWOCAPTCHA_API_KEY no configurada")

    with httpx.Client(timeout=30) as client:
        r = client.post(_2CAPTCHA_SUBMIT, data={
            "key": tc_key, "method": "base64", "body": imagen_base64,
            "min_len": min_len, "max_len": max_len, "json": 1,
        })
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 1:
            raise RuntimeError(f"2captcha submit error: {data.get('request')}")
        task_id = data["request"]

        # ponytail: techo 60s (3s × 20). Un image-captcha normal resuelve en <20s;
        # más que eso ya rozaría el timeout de 300s del job con reintentos.
        for _ in range(20):
            time.sleep(3)
            r = client.get(_2CAPTCHA_RESULT, params={
                "key": tc_key, "action": "get", "id": task_id, "json": 1,
            })
            data = r.json()
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                raise RuntimeError(f"2captcha error: {data.get('request')}")

    raise RuntimeError("2captcha: timeout esperando solución de imagen")


if __name__ == "__main__":
    # Self-check del presupuesto, sin red ni gasto: con presupuesto agotado no debe
    # arrancar ningún solve, y debe fallar rápido en vez de colgarse hasta el timeout
    # del llamador (que es exactamente el bug que se está corrigiendo).
    os.environ.pop("TWOCAPTCHA_API_KEY", None)
    t = time.monotonic()
    try:
        resolver_recaptcha("sitekey-falsa", "https://example.com", "CAP-falsa", presupuesto_s=0)
        raise AssertionError("debió lanzar RuntimeError")
    except RuntimeError as e:
        assert "TWOCAPTCHA_API_KEY no configurada" in str(e), e
    transcurrido = time.monotonic() - t
    assert transcurrido < 2, f"tardó {transcurrido:.1f}s: el presupuesto no se respetó"
    print(f"OK: presupuesto respetado ({transcurrido:.3f}s, 0 llamadas de red)")
