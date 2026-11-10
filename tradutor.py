"""
Tradução das falas, sempre 1:1 (uma tradução para cada fala, para casar com os tempos).

Motores, na ordem em que são tentados:
  1. Google  (translate.googleapis.com, cliente "gtx"): um lote de falas separadas por quebra de linha
  2. Microsoft (o mesmo serviço usado pelo tradutor do Edge): um lote de falas, alinhado por item
  3. Google fala por fala: mais lento; só entra se o lote do Google desalinhar e a Microsoft falhar

O motor que funcionar passa a ser o primeiro da fila para os próximos lotes.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("legendas")

TIMEOUT = (6, 20)  # segundos: (conectar, ler)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
EDGE_AUTH_URL = "https://edge.microsoft.com/translate/auth"
MICROSOFT_URL = "https://api.cognitive.microsofttranslator.com/translate"


class TranslateError(Exception):
    """Falha ao traduzir. A mensagem já vem pronta para mostrar ao usuário."""


class Misaligned(TranslateError):
    """O tradutor juntou ou quebrou falas, então não dá para casar com os tempos."""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
_tls = threading.local()


def _http() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = _tls.session = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
    return s


def _request(method: str, url: str, who: str, **kw) -> requests.Response:
    try:
        r = _http().request(method, url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        raise TranslateError(f"{who}: falha de conexão ({type(e).__name__})") from e
    if r.status_code == 429:
        raise TranslateError(f"{who}: limite de uso atingido (HTTP 429)")
    if r.status_code >= 400:
        log.warning("%s HTTP %s: %r", who, r.status_code, r.text[:300])
        raise TranslateError(f"{who}: HTTP {r.status_code}")
    return r


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# --------------------------------------------------------------------------- #
# Google
# --------------------------------------------------------------------------- #
def google_text(text: str, src: str, tgt: str) -> str:
    r = _request(
        "GET", GOOGLE_URL, "Google",
        params={"client": "gtx", "sl": src, "tl": tgt, "dt": "t",
                "ie": "UTF-8", "oe": "UTF-8", "q": text},
    )
    try:
        data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and isinstance(seg[0], str))
    except Exception as e:
        log.warning("Google: resposta inesperada: %r", r.text[:300])
        raise TranslateError("Google: resposta em formato inesperado") from e


def google_batch(lines: list[str], src: str, tgt: str) -> list[str]:
    out = google_text("\n".join(lines), src, tgt)
    parts = [p.strip() for p in re.split(r"\r?\n", out.strip())]
    if len(parts) != len(lines):
        raise Misaligned(f"Google: enviei {len(lines)} falas e voltaram {len(parts)}")
    return parts


def google_one_by_one(lines: list[str], src: str, tgt: str) -> list[str]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        return [_one_line(t) for t in pool.map(lambda s: google_text(s, src, tgt), lines)]


# --------------------------------------------------------------------------- #
# Microsoft
# --------------------------------------------------------------------------- #
_ms_lock = threading.Lock()
_ms_token = {"value": "", "until": 0.0}


def _microsoft_token(renew: bool = False) -> str:
    with _ms_lock:
        if renew or not _ms_token["value"] or time.time() >= _ms_token["until"]:
            tok = _request("GET", EDGE_AUTH_URL, "Microsoft").text.strip()
            if len(tok) < 20 or " " in tok:
                raise TranslateError("Microsoft: não consegui o token de acesso")
            _ms_token.update(value=tok, until=time.time() + 8 * 60)
        return _ms_token["value"]


def microsoft_batch(lines: list[str], src: str, tgt: str) -> list[str]:
    params = {"api-version": "3.0", "to": tgt}  # "pt" = português do Brasil
    if src != "auto":
        params["from"] = src
    body = [{"Text": t} for t in lines]
    r = None
    for renew in (False, True):
        headers = {"Authorization": "Bearer " + _microsoft_token(renew)}
        try:
            r = _request("POST", MICROSOFT_URL, "Microsoft", params=params, json=body, headers=headers)
            break
        except TranslateError as e:
            if renew or "HTTP 401" not in str(e):  # 401 = token vencido: renova 1 vez
                raise
    try:
        out = [_one_line(item["translations"][0]["text"]) for item in r.json()]
    except Exception as e:
        log.warning("Microsoft: resposta inesperada: %r", r.text[:300])
        raise TranslateError("Microsoft: resposta em formato inesperado") from e
    if len(out) != len(lines):
        raise Misaligned(f"Microsoft: enviei {len(lines)} falas e voltaram {len(out)}")
    return out


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #
ENGINES = {"Google": google_batch, "Microsoft": microsoft_batch}


class Translator:
    def __init__(self, src: str, tgt: str):
        self.src, self.tgt = src, tgt
        self.order = list(ENGINES)
        self.used: set[str] = set()

    def translate(self, lines: list[str]) -> list[str]:
        errors, google_misaligned = [], False
        for name in list(self.order):
            try:
                out = ENGINES[name](lines, self.src, self.tgt)
                if not any(out):
                    raise TranslateError(f"{name}: a tradução veio vazia")
            except TranslateError as e:
                google_misaligned |= name == "Google" and isinstance(e, Misaligned)
                log.warning("%s", e)
                errors.append(str(e))
                continue
            if self.order[0] != name:  # quem funcionou vai para o começo da fila
                self.order.remove(name)
                self.order.insert(0, name)
            self.used.add(name)
            return out

        if google_misaligned:  # o Google responde, só não manteve as quebras de linha
            try:
                out = google_one_by_one(lines, self.src, self.tgt)
                self.used.add("Google")
                return out
            except TranslateError as e:
                log.warning("%s", e)
                errors.append(str(e))

        raise TranslateError(" · ".join(errors))
