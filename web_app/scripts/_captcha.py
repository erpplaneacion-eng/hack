# -*- coding: utf-8 -*-
"""Resolución de reCAPTCHA Enterprise: CapSolver → 2captcha fallback.
Síncrono, diseñado para asyncio.to_thread."""
import os
import time

import capsolver
import httpx

_2CAPTCHA_SUBMIT = "https://2captcha.com/in.php"
_2CAPTCHA_RESULT = "https://2captcha.com/res.php"


def resolver_recaptcha(sitekey: str, url: str, capsolver_key: str, page_action: str = "verify") -> str:
    # 1. CapSolver
    capsolver.api_key = capsolver_key
    for tipo in ["ReCaptchaV2EnterpriseTaskProxyless", "ReCaptchaV3TaskProxyless", "ReCaptchaV2TaskProxyless"]:
        payload = {"type": tipo, "websiteURL": url, "websiteKey": sitekey}
        if tipo == "ReCaptchaV3TaskProxyless":
            payload["pageAction"] = page_action
        for _ in range(3):
            try:
                token = capsolver.solve(payload).get("gRecaptchaResponse", "")
                if token:
                    return token
                break
            except Exception:
                pass

    # 2. Fallback: 2captcha
    tc_key = os.getenv("TWOCAPTCHA_API_KEY", "")
    if not tc_key:
        raise RuntimeError("CapSolver agotado y TWOCAPTCHA_API_KEY no configurada")

    with httpx.Client(timeout=30) as client:
        r = client.post(_2CAPTCHA_SUBMIT, data={
            "key": tc_key, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": url,
            "enterprise": 1, "json": 1,
        })
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 1:
            raise RuntimeError(f"2captcha submit error: {data.get('request')}")
        task_id = data["request"]

        for _ in range(24):  # max ~2 min
            time.sleep(5)
            r = client.get(_2CAPTCHA_RESULT, params={
                "key": tc_key, "action": "get", "id": task_id, "json": 1,
            })
            data = r.json()
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                raise RuntimeError(f"2captcha error: {data.get('request')}")

    raise RuntimeError("2captcha: timeout esperando solución")


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
