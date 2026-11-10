"""
Tradução das falas, sempre 1:1 (uma tradução para cada fala, para casar com os tempos).

Primeiro o Google Tradutor gratuito (translate.googleapis.com, cliente "gtx"), em até três modos:
  1. Google: as falas vão num texto só, uma por linha. É o modo com mais contexto, mas às vezes o
     Google junta duas falas numa linha e o lote deixa de casar com os tempos.
  2. Google (itens): o mesmo lote num pedido só, com cada fala num item separado (translate_a/t).
     A resposta traz sempre uma tradução por item; só se perde o contexto entre uma fala e outra.
  3. Google (fala por fala): um pedido por fala, mais lento. Só entra quando os modos 1 e 2 falham
     e um deles desalinhou (sinal de que o Google está respondendo).
Sem conexão ou com o limite de uso atingido (HTTP 429), os outros modos do Google nem são tentados.

Bloqueio do Google (HTTP 429): nenhum pedido sai até passar a espera, para não prolongar o bloqueio.
A espera é a do cabeçalho Retry-After, se vier; senão 2, 5, 10 e depois 15 min a cada 429 seguido.
Quem chama recebe RateLimited, com a hora em que vale tentar de novo (até lá, nem adianta).
Depois do primeiro bloqueio, os pedidos passam a sair no máximo 1 a cada 2 s.

Reserva, só quando o Google falha: MyMemory (api.mymemory.translated.net), sem chave e sem e-mail.
  - Cota de 5.000 caracteres por dia e no máximo 500 bytes por pedido. As falas vão em lotes de até
    500 bytes, uma por linha; se a contagem de linhas não bater, o lote é dividido ao meio até casar.
  - Não detecta o idioma: precisa do código da legenda (source_lang do Translator).
  - Quando a cota acaba, fica de fora até a hora em que o MyMemory diz que ela volta.
  - No máximo 1 pedido por segundo.
A reserva da Microsoft saiu: o token gratuito do Edge (edge.microsoft.com/translate/auth) responde 404
desde novembro de 2026, e o Bing Tradutor web junta as falas e aceita só 1000 caracteres por pedido.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("legendas")

TIMEOUT = (6, 20)  # segundos: (conectar, ler)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"   # um texto -> trechos traduzidos
GOOGLE_ITEMS_URL = "https://translate.googleapis.com/translate_a/t"  # vários "q" -> uma tradução por "q"
BLOCK_WAITS = (120, 300, 600, 900)  # espera (s) após o 1º, 2º, 3º, 4º+ HTTP 429 seguidos, sem Retry-After
PACE_AFTER_BLOCK = 2.0              # depois de um bloqueio, no máximo 1 pedido a cada 2 s
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_MAX_BYTES = 500            # tamanho máximo do "q" (UTF-8)
MYMEMORY_PACE = 1.0                 # no máximo 1 pedido por segundo
MYMEMORY_DAILY_CHARS = 5000         # cota grátis por dia, sem e-mail (só para as mensagens)


class TranslateError(Exception):
    """Falha ao traduzir. A mensagem já vem pronta para mostrar ao usuário."""


class Misaligned(TranslateError):
    """O tradutor juntou ou quebrou falas, então não dá para casar com os tempos."""


class Unavailable(TranslateError):
    """Sem conexão ou limite de uso atingido: outro modo do mesmo serviço também falharia."""


class RateLimited(Unavailable):
    """O Google barrou este IP por excesso de pedidos (HTTP 429). Nenhum pedido sai antes de `until`."""

    def __init__(self, who: str, until: float):
        super().__init__(f"{who}: limite de uso atingido (HTTP 429)")
        self.until = until  # time.time() a partir do qual vale tentar de novo
        self.fallback = ""  # por que a reserva (MyMemory) também não traduziu, se foi tentada


class QuotaExceeded(TranslateError):
    """A cota grátis do MyMemory acabou: nenhum pedido sai antes de `until`."""

    def __init__(self, until: float):
        super().__init__(f"MyMemory (reserva): a cota grátis do dia acabou; volta {clock(until)}")
        self.until = until


def clock(ts: float) -> str:
    """'às 15:30', 'amanhã às 09:12' ou 'em 12/11 às 09:12'."""
    t, now = time.localtime(ts), time.localtime()
    hm = time.strftime("%H:%M", t)
    if (t.tm_year, t.tm_yday) == (now.tm_year, now.tm_yday):
        return f"às {hm}"
    tomorrow = time.localtime(time.time() + 86400)
    if (t.tm_year, t.tm_yday) == (tomorrow.tm_year, tomorrow.tm_yday):
        return f"amanhã às {hm}"
    return time.strftime("em %d/%m às %H:%M", t)


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


# Bloqueio do Google (HTTP 429): vale para todos os vídeos e threads do app.
_gate_lock = threading.Lock()
_gate = {"until": 0.0, "strikes": 0, "paced": False, "next": 0.0}


def _retry_after(value: str | None) -> float | None:
    """Segundos pedidos no cabeçalho Retry-After (em segundos ou como data HTTP)."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return seconds if seconds == seconds else None  # NaN não vale


def _wait_turn(who: str) -> None:
    """Antes de cada pedido: recusa se o Google está bloqueando; depois de um bloqueio, espaça os pedidos."""
    with _gate_lock:
        now = time.time()
        if now < _gate["until"]:
            raise RateLimited(who, _gate["until"])
        start = max(now, _gate["next"]) if _gate["paced"] else now
        _gate["next"] = start + PACE_AFTER_BLOCK
    if start > now:
        time.sleep(start - now)
        with _gate_lock:
            if time.time() < _gate["until"]:  # outro pedido levou 429 enquanto este esperava a vez
                raise RateLimited(who, _gate["until"])


def _blocked(who: str, retry_after: str | None) -> RateLimited:
    """Anota o HTTP 429 e devolve o erro com a hora da próxima tentativa."""
    with _gate_lock:
        now = time.time()
        if now >= _gate["until"]:  # 429 novo (não de um pedido que saiu antes do bloqueio já anotado)
            _gate["strikes"] += 1
            wait = _retry_after(retry_after)
            if wait is None:
                wait = BLOCK_WAITS[min(_gate["strikes"], len(BLOCK_WAITS)) - 1]
            _gate["until"] = now + min(max(wait, 5.0), 3600.0)
            _gate["paced"] = True
        return RateLimited(who, _gate["until"])


def _send(method: str, url: str, who: str, **kw) -> requests.Response:
    """Manda o pedido; sem resposta (sem conexão, tempo esgotado...) vira Unavailable."""
    try:
        return _http().request(method, url, timeout=TIMEOUT, **kw)
    except requests.RequestException as e:
        raise Unavailable(f"{who}: falha de conexão ({type(e).__name__})") from e


def _request(method: str, url: str, who: str, **kw) -> requests.Response:
    """Pedido ao Google, respeitando o bloqueio (HTTP 429)."""
    _wait_turn(who)
    r = _send(method, url, who, **kw)
    if r.status_code == 429:
        err = _blocked(who, r.headers.get("Retry-After"))
        log.warning("%s; nenhum pedido ao Google antes das %s",
                    err, time.strftime("%H:%M:%S", time.localtime(err.until)))
        raise err
    with _gate_lock:
        _gate["strikes"] = 0  # o Google voltou a responder: o próximo 429 recomeça pela espera menor
    if r.status_code >= 400:
        log.warning("%s HTTP %s: %r", who, r.status_code, r.text[:300])
        raise TranslateError(f"{who}: HTTP {r.status_code}")
    return r


_INVISIBLE = dict.fromkeys([0x200B, 0xFEFF])  # espaço de largura zero e BOM, que o Google às vezes põe no texto


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_INVISIBLE)).strip()


# --------------------------------------------------------------------------- #
# Google
# --------------------------------------------------------------------------- #
def google_text(text: str, src: str, tgt: str, who: str = "Google") -> str:
    r = _request(
        "GET", GOOGLE_URL, who,
        params={"client": "gtx", "sl": src, "tl": tgt, "dt": "t",
                "ie": "UTF-8", "oe": "UTF-8", "q": text},
    )
    try:
        data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and isinstance(seg[0], str))
    except Exception as e:
        log.warning("%s: resposta inesperada: %r", who, r.text[:300])
        raise TranslateError(f"{who}: resposta em formato inesperado") from e


def google_batch(lines: list[str], src: str, tgt: str) -> list[str]:
    """Modo 1: as falas num texto só, uma por linha."""
    out = google_text("\n".join(lines), src, tgt)
    parts = [_one_line(p) for p in re.split(r"\r?\n", out.strip())]
    if len(parts) != len(lines):
        raise Misaligned(f"Google: enviei {len(lines)} falas e voltaram {len(parts)}")
    return parts


def google_items(lines: list[str], src: str, tgt: str) -> list[str]:
    """Modo 2: o lote num pedido só, com cada fala num "q" separado."""
    who = "Google (itens)"
    r = _request("POST", GOOGLE_ITEMS_URL, who,
                 params={"client": "gtx", "sl": src, "tl": tgt},
                 data=[("q", t) for t in lines])
    try:
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("esperava uma lista")
        # idioma de origem fixo: ["tradução", ...]; "auto": [["tradução", "idioma detectado"], ...]
        texts = [item[0] if isinstance(item, list) and item else item for item in data]
        if not all(isinstance(t, str) for t in texts):
            raise ValueError("esperava só textos")
    except Exception as e:
        log.warning("%s: resposta inesperada: %r", who, r.text[:300])
        raise TranslateError(f"{who}: resposta em formato inesperado") from e
    if len(texts) != len(lines):
        raise Misaligned(f"{who}: enviei {len(lines)} falas e voltaram {len(texts)}")
    return [_one_line(t) for t in texts]


def google_one_by_one(lines: list[str], src: str, tgt: str) -> list[str]:
    """Modo 3: um pedido por fala (4 de cada vez)."""
    def one(text: str) -> str:
        return _one_line(google_text(text, src, tgt, "Google (fala por fala)"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, t) for t in lines]
        try:
            return [f.result() for f in futures]
        except TranslateError:
            for f in futures:  # deu erro: não manda os pedidos que ainda estão na fila
                f.cancel()
            raise


# --------------------------------------------------------------------------- #
# MyMemory (reserva)
# --------------------------------------------------------------------------- #
_mm_lock = threading.Lock()
_mm = {"until": 0.0, "next": 0.0}  # a cota acabou até `until`; o próximo pedido não sai antes de `next`

# códigos de legenda do YouTube que o MyMemory escreve de outro jeito
_MM_CODES = {"zh-hans": "zh-CN", "zh-hant": "zh-TW", "zh-tw": "zh-TW", "zh-hk": "zh-TW",
             "zh": "zh-CN", "iw": "he", "in": "id", "ji": "yi", "jw": "jv", "pt": "pt-BR"}


def mymemory_lang(code: str | None) -> str | None:
    """Código de idioma do YouTube ("en-US", "es-419", "zh-Hans"...) no formato do MyMemory.
    None para "auto" ou vazio: o MyMemory não detecta o idioma sozinho."""
    if not code or code.lower() == "auto":
        return None
    low = code.lower()
    base = low.split("-")[0]
    return _MM_CODES.get(low) or _MM_CODES.get(base) or base


def _mymemory_turn() -> None:
    """Antes de cada pedido ao MyMemory: recusa se a cota acabou; no máximo 1 pedido por segundo."""
    with _mm_lock:
        now = time.time()
        if now < _mm["until"]:
            raise QuotaExceeded(_mm["until"])
        start = max(now, _mm["next"])
        _mm["next"] = start + MYMEMORY_PACE
    if start > now:
        time.sleep(start - now)


def _quota_over(warning: str) -> QuotaExceeded:
    """Anota que a cota acabou, até a hora do aviso ("NEXT AVAILABLE IN 10 HOURS 20 MINUTES 33 SECONDS")."""
    m = re.search(r"NEXT AVAILABLE IN\s*((?:\d+\s*(?:HOUR|MINUTE|SECOND)S?\s*)+)", warning or "", re.I)
    units = {"HOUR": 3600, "MINUTE": 60, "SECOND": 1}
    wait = sum(int(n) * units[u.upper()]
               for n, u in re.findall(r"(\d+)\s*(HOUR|MINUTE|SECOND)", m[1] if m else "", re.I))
    with _mm_lock:
        _mm["until"] = max(_mm["until"], time.time() + max(wait or 3600, 60))  # sem hora: tenta em 1 h
        err = QuotaExceeded(_mm["until"])
    log.warning("%s", err)
    return err


def mymemory_text(text: str, src: str, tgt: str) -> str:
    """Um pedido ao MyMemory (até 500 bytes). src e tgt já no formato do MyMemory."""
    who = "MyMemory (reserva)"
    _mymemory_turn()
    r = _send("GET", MYMEMORY_URL, who, params={"q": text, "langpair": f"{src}|{tgt}"})
    try:
        data = r.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        if r.status_code == 429:
            raise _quota_over("")
        log.warning("%s HTTP %s: %r", who, r.status_code, r.text[:300])
        raise TranslateError(f"{who}: HTTP {r.status_code}" if r.status_code >= 400
                             else f"{who}: resposta em formato inesperado")
    found = (data.get("responseData") or {}).get("translatedText")
    details = str(data.get("responseDetails") or "")
    try:
        status = int(data.get("responseStatus") or r.status_code)
    except (TypeError, ValueError):
        status = r.status_code
    # o HTTP pode vir 200 com o erro dentro do JSON, e aí translatedText é a própria mensagem de erro
    warning = f"{details} {found if isinstance(found, str) else ''}"
    if (data.get("quotaFinished") or 429 in (status, r.status_code)
            or "USED ALL AVAILABLE FREE TRANSLATIONS" in warning.upper()):
        raise _quota_over(warning)
    if status != 200 or r.status_code >= 400 or not isinstance(found, str):
        log.warning("%s: status %s: %r", who, status, warning[:300])
        raise TranslateError(f"{who}: {details[:120] or 'resposta sem tradução'} (status {status})")
    return html.unescape(found)


def _byte_groups(lines: list[str], limit: int) -> list[list[str]]:
    """Junta falas seguidas em grupos que, unidos por quebra de linha, cabem em `limit` bytes (UTF-8)."""
    groups, cur, size = [], [], 0
    for t in lines:
        n = len(t.encode("utf-8"))
        if cur and size + 1 + n > limit:
            groups.append(cur)
            cur, size = [], 0
        size += n + (1 if cur else 0)
        cur.append(t)
    if cur:
        groups.append(cur)
    return groups


def _mymemory_lines(lines: list[str], src: str, tgt: str) -> list[str]:
    """Um lote, uma fala por linha. Se a contagem não bater, divide ao meio (no fim, uma fala por pedido)."""
    out = mymemory_text("\n".join(lines), src, tgt)
    if len(lines) == 1:
        return [_one_line(out)]
    parts = [_one_line(p) for p in re.split(r"\r?\n", out.strip())]
    if len(parts) == len(lines):
        return parts
    log.info("MyMemory (reserva): enviei %d falas e voltaram %d; dividindo o lote", len(lines), len(parts))
    mid = len(lines) // 2
    return _mymemory_lines(lines[:mid], src, tgt) + _mymemory_lines(lines[mid:], src, tgt)


def mymemory_batch(lines: list[str], src: str, tgt: str) -> list[str]:
    """Reserva: as falas em lotes de até 500 bytes, sempre uma tradução por fala.
    src e tgt como no resto do app ("en", "pt", ou o código da legenda, ex. "es-419")."""
    pair = mymemory_lang(src), mymemory_lang(tgt)
    if not all(pair):
        raise TranslateError("MyMemory (reserva): precisa saber o idioma de origem da legenda")
    out: list[str] = []
    for group in _byte_groups(lines, MYMEMORY_MAX_BYTES):
        out += _mymemory_lines(group, *pair)
    return out


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #
# Modos do Google tentados em ordem a cada lote (o diagnóstico, app.py --diagnostico, testa cada um).
# O modo 3 (fala por fala) fica de fora: só entra no fim, se o Google responder mas desalinhar.
ENGINES = {"Google": google_batch, "Google (itens)": google_items}
RESERVE = {"MyMemory (reserva)": mymemory_batch}  # só quando o Google falha


class Translator:
    def __init__(self, src: str, tgt: str, source_lang: str | None = None):
        """src: origem para o Google ("en", "pt" ou "auto"); tgt: "pt" ou "en".
        source_lang: código da legenda no YouTube (ex. "es-419"), para a reserva, que não detecta o idioma."""
        self.src, self.tgt = src, tgt
        self.source_lang = source_lang or src
        self.used: set[str] = set()  # serviços que traduziram algum lote (vai na mensagem final)
        self.last = ""               # quem traduziu o último lote: "Google" ou "MyMemory"
        self.google_blocked = False  # o último lote foi para a reserva porque o Google está bloqueando

    def translate(self, lines: list[str]) -> list[str]:
        """Devolve uma tradução por fala, na mesma ordem, ou levanta TranslateError.
        Tenta o Google e, se ele falhar, a reserva (MyMemory). Se o Google está bloqueando e a reserva
        também não traduziu, levanta RateLimited: `until` diz quando tentar o Google e `fallback`, por que
        a reserva não serviu."""
        try:
            out, last = self._google(lines), "Google"
        except TranslateError as e:
            out, last = self._reserve(lines, e), "MyMemory"
            self.google_blocked = isinstance(e, RateLimited)
        self.last = last
        self.used.add(last)
        return out

    def _reserve(self, lines: list[str], google_error: TranslateError) -> list[str]:
        try:
            out = mymemory_batch(lines, self.source_lang, self.tgt)
            if not any(out):
                raise TranslateError("MyMemory (reserva): a tradução veio vazia")
        except TranslateError as e:
            if not isinstance(e, QuotaExceeded):  # cota: o aviso já foi para o log uma vez
                log.warning("%s", e)
            if isinstance(google_error, RateLimited):
                google_error.fallback = str(e)
                raise google_error from e
            raise TranslateError(f"{google_error} · {e}") from e
        log.info("MyMemory (reserva): lote de %d falas traduzido (%s)", len(lines), google_error)
        return out

    def _google(self, lines: list[str]) -> list[str]:
        errors, misaligned = [], False
        for name, engine in ENGINES.items():
            try:
                out = engine(lines, self.src, self.tgt)
                if not any(out):
                    raise TranslateError(f"{name}: a tradução veio vazia")
            except Unavailable as e:  # mesmo serviço: tentar outro modo só gastaria mais pedidos
                if not isinstance(e, RateLimited):  # o 429 já foi para o log quando chegou
                    log.warning("%s", e)
                raise
            except TranslateError as e:
                misaligned |= isinstance(e, Misaligned)
                log.warning("%s", e)
                errors.append(str(e))
                continue
            if errors:
                log.info("%s: lote de %d falas traduzido", name, len(lines))
            return out

        if misaligned:  # o Google responde, só não manteve as falas separadas
            try:
                return google_one_by_one(lines, self.src, self.tgt)
            except Unavailable as e:
                if not isinstance(e, RateLimited):
                    log.warning("%s", e)
                raise
            except TranslateError as e:
                log.warning("%s", e)
                errors.append(str(e))

        raise TranslateError(" · ".join(errors))
