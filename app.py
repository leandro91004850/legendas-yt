"""
Legendas YT — tradução em tempo real de legendas do YouTube (Português <> Inglês).

Abre o vídeo dentro do app, baixa a legenda original do YouTube e mostra,
sincronizada com o vídeo, a linha original + a tradução.

Uso:
    python app.py                       # abre o app vazio
    python app.py "https://youtu.be/…"  # já abre com o vídeo
    python app.py --diagnostico [link]  # testa tradutores e legendas, sem abrir janela
"""
from __future__ import annotations

import functools
import json
import logging
import re
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import requests
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

from tradutor import ENGINES, MYMEMORY_DAILY_CHARS, RESERVE, RateLimited, TranslateError, Translator

BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_DIR = BASE_DIR / "ui"
PREFERRED_PORT = 8765      # porta fixa = preferências (tamanho da fonte) persistem
CHUNK_CHARS = 1200         # tamanho de cada lote enviado ao tradutor
CHUNK_LINES = 40
RETRY_WAITS = (5, 15, 30)  # novas tentativas para lotes que falharam (segundos)

log = logging.getLogger("legendas")


def setup_logging() -> Path:
    folder = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    path = folder / "legendas-yt.log"
    try:
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        path = Path(tempfile.gettempdir()) / "legendas-yt.log"
        handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stderr:
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)
    return path


# --------------------------------------------------------------------------- #
# Legendas
# --------------------------------------------------------------------------- #
class TimeoutSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", (6, 20))
        return super().request(*args, **kwargs)


def transcript_api() -> YouTubeTranscriptApi:
    return YouTubeTranscriptApi(http_client=TimeoutSession())


def extract_video_id(text: str) -> str | None:
    text = (text or "").strip()
    if re.fullmatch(r"[\w-]{11}", text):
        return text
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([\w-]{11})", text)
    return m.group(1) if m else None


def lang_family(code: str) -> str:
    return code.split("-")[0].lower()


def target_for(source_code: str) -> str:
    """Inglês -> português; português -> inglês; qualquer outra -> português."""
    return "en" if lang_family(source_code) == "pt" else "pt"


def pick_transcript(tracks, source: str):
    """
    Escolhe a melhor faixa de legenda.
    source = "auto" | "en" | "pt"
      auto: prefere legenda feita por humanos (EN ou PT) à gerada automaticamente.
      en/pt: prefere esse idioma, mesmo que seja gerado automaticamente.
    """
    tracks = list(tracks)
    if not tracks:
        raise NoTranscriptFound("", [], None)
    order = ["pt", "en"] if source == "pt" else ["en", "pt"]

    def score(t):
        fam = lang_family(t.language_code)
        rank = order.index(fam) if fam in order else len(order)
        if source == "auto":
            return (rank == len(order), t.is_generated, rank)
        return (rank, t.is_generated)

    return min(tracks, key=score)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def language_name(track) -> str:
    """'English (auto-generated)' -> 'English'."""
    return re.sub(r"\s*\(.*?\)\s*$", "", track.language).strip() or track.language_code


def make_chunks(items):
    """Agrupa [(índice, texto), ...] em lotes de até CHUNK_CHARS caracteres / CHUNK_LINES falas."""
    chunks, cur, size = [], [], 0
    for idx, text in items:
        if cur and (size + len(text) + 1 > CHUNK_CHARS or len(cur) >= CHUNK_LINES):
            chunks.append(cur)
            cur, size = [], 0
        cur.append((idx, text))
        size += len(text) + 1
    if cur:
        chunks.append(cur)
    return chunks


def friendly_error(exc: Exception) -> str:
    if isinstance(exc, TranscriptsDisabled):
        return "Este vídeo não tem legendas (nem automáticas)."
    if isinstance(exc, NoTranscriptFound):
        return "Nenhuma legenda encontrada para este vídeo."
    if isinstance(exc, VideoUnavailable):
        return "Vídeo indisponível."
    if isinstance(exc, AgeRestricted):
        return "Vídeo com restrição de idade: legendas inacessíveis sem login."
    if isinstance(exc, (RequestBlocked, IpBlocked)):
        return "O YouTube bloqueou temporariamente as requisições deste IP. Tente mais tarde."
    return f"Erro ao buscar legendas: {type(exc).__name__}"


# --------------------------------------------------------------------------- #
# Ponte Python <-> interface
# --------------------------------------------------------------------------- #
class Api:
    def __init__(self, log_path: Path | None = None):
        self._window = None
        self._job = 0
        self._focus = 0
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[dict, list, str]] = {}
        self._log_name = log_path.name if log_path else "legendas-yt.log"

    # ---- chamados pelo JavaScript ----
    def load(self, url, source="auto"):
        vid = extract_video_id(url)
        if not vid:
            return {"ok": False, "error": "Link do YouTube inválido."}
        with self._lock:
            self._job += 1
            job = self._job
            self._focus = 0
        threading.Thread(target=self._work, args=(job, vid, source), daemon=True).start()
        return {"ok": True, "videoId": vid, "job": job}

    def focus(self, job, index):
        """A interface avisa onde o vídeo está, para traduzir esse trecho primeiro."""
        if job == self._job:
            self._focus = int(index)

    def toggle_fullscreen(self):
        self._window.toggle_fullscreen()

    # ---- internos ----
    def _alive(self, job):
        return job == self._job

    def _js(self, fn, *args):
        if self._window:
            payload = ", ".join(json.dumps(a, ensure_ascii=True) for a in args)
            self._window.evaluate_js(f"window.app && window.app.{fn}({payload})")

    def _sleep(self, job, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if not self._alive(job):
                return False
            time.sleep(0.25)
        return self._alive(job)

    def _wait_block(self, job, until, fallback=""):
        """O Google bloqueou (HTTP 429) e a reserva não deu conta (`fallback` diz por quê):
        espera sem mandar pedidos e mostra quando tenta o Google de novo."""
        while True:
            left = until - time.time()
            if left <= 0:
                return self._alive(job)
            when = time.strftime("%H:%M", time.localtime(until))
            mins = "menos de 1 min" if left < 60 else f"{int((left + 59) // 60)} min"
            reserve = f" {fallback}." if fallback else ""
            self._js("setStatus", job,
                     "O Google bloqueou temporariamente as traduções por excesso de pedidos (HTTP 429)."
                     f"{reserve} Nova tentativa no Google às {when} (em {mins}); a tradução continua sozinha.",
                     True)
            if not self._sleep(job, min(left, 15)):
                return False

    def _next_chunk(self, pending, chunks):
        after = [c for c in pending if chunks[c][-1][0] >= self._focus]
        return min(after) if after else min(pending)

    def _work(self, job, vid, source):
        key = (vid, source)
        if key in self._cache:
            meta, translations, done_msg = self._cache[key]
            self._js("onTranscript", job, meta)
            self._js("onTranslations", job, translations)
            self._js("setStatus", job, done_msg)
            return

        # 1) legenda original
        try:
            self._js("setStatus", job, "Buscando legendas…")
            track = pick_transcript(transcript_api().list(vid), source)
            cues = [
                {"start": s.start, "dur": s.duration, "text": clean(s.text)}
                for s in track.fetch()
            ]
        except Exception as exc:
            log.warning("Legendas de %s: %r", vid, exc)
            if self._alive(job):
                self._js("setStatus", job, friendly_error(exc), True)
            return
        if not self._alive(job):
            return

        src_code = lang_family(track.language_code)
        tgt = target_for(track.language_code)
        meta = {
            "cues": cues,
            "sourceName": language_name(track),
            "sourceCode": track.language_code,
            "generated": track.is_generated,
            "target": tgt,
            "targetName": "Português" if tgt == "pt" else "English",
        }
        log.info("Vídeo %s: %d falas em %s (%s) -> %s",
                 vid, len(cues), track.language_code, "auto" if track.is_generated else "manual", tgt)
        self._js("onTranscript", job, meta)
        self._js("setStatus", job, f"Traduzindo para {meta['targetName']}…")

        # 2) tradução em lotes, começando pelo trecho onde o vídeo está
        items = [(i, c["text"]) for i, c in enumerate(cues) if c["text"]]
        chunks = make_chunks(items)
        translator = Translator(src_code if src_code in ("en", "pt") else "auto", tgt, track.language_code)
        collected, pending, last_error = [], list(range(len(chunks))), ""

        for wait in (0, *RETRY_WAITS):
            if not pending:
                break
            if wait:
                self._js("setStatus", job, f"{last_error} — tentando de novo em {wait} s…", True)
                if not self._sleep(job, wait):
                    return
            failed, streak = [], 0
            while pending:
                if not self._alive(job):
                    return
                cid = self._next_chunk(pending, chunks)
                pending.remove(cid)
                chunk = chunks[cid]
                try:
                    out = translator.translate([t for _, t in chunk])
                except RateLimited as e:  # o Google bloqueou e a reserva não deu conta: espera e retoma sozinho
                    pending.append(cid)
                    if not self._wait_block(job, e.until, e.fallback):
                        return
                    continue
                except TranslateError as e:
                    last_error = str(e)
                    failed.append(cid)
                    streak += 1
                    self._js("setStatus", job, f"Falha ao traduzir: {e}", True)
                    if streak >= 2:  # serviço fora do ar: espera um pouco antes de insistir
                        failed += pending
                        pending = []
                    continue
                streak = 0
                pairs = [[idx, t] for (idx, _), t in zip(chunk, out)]
                collected.extend(pairs)
                if self._alive(job):
                    self._js("onTranslations", job, pairs)
                    if translator.last == "MyMemory":  # a interface acabou de pôr "Traduzindo… N%"
                        why = ("o Google bloqueou temporariamente" if translator.google_blocked
                               else "o Google falhou")
                        daily = f"{MYMEMORY_DAILY_CHARS:,}".replace(",", ".")
                        self._js("setStatus", job,
                                 f"Traduzindo pelo MyMemory (reserva, até {daily} caracteres por dia), "
                                 f"porque {why}… {round(100 * len(collected) / len(items))}%")
            pending = failed

        if not self._alive(job):
            return
        if pending:
            missing = sum(len(chunks[c]) for c in pending)
            msg = (f"{missing} falas ficaram sem tradução ({last_error}). "
                   f"Clique em Abrir para tentar de novo. Detalhes em {self._log_name}")
            log.warning(msg)
            self._js("setStatus", job, msg, True)
            return
        engines = " + ".join(sorted(translator.used)) or "—"
        done_msg = f"{meta['sourceName']} → {meta['targetName']} ✓ ({engines})"
        self._cache[key] = (meta, collected, done_msg)
        self._js("setStatus", job, done_msg)


# --------------------------------------------------------------------------- #
# Diagnóstico (python app.py --diagnostico [link])
# --------------------------------------------------------------------------- #
def diagnose(url: str | None) -> int:
    print("Legendas YT — diagnóstico\n")
    samples = ["Hello, how are you?", "I started listening more.", "Do you understand English but feel"]
    def check(name, fn):
        """Traduz as amostras; devolve (deu certo?, hora até quando o Google bloqueia ou None)."""
        t0 = time.time()
        try:
            out = fn(samples, "en", "pt")
            print(f"[OK]    {name}, {time.time() - t0:.1f} s: {out}")
            return True, None
        except Exception as e:
            print(f"[FALHA] {e}" if isinstance(e, TranslateError) else f"[FALHA] {name}: {e}")
            return False, e.until if isinstance(e, RateLimited) else None

    results = [check(name, fn) for name, fn in ENGINES.items()]  # os modos do Google, na ordem do app
    blocked = max((until for _, until in results if until), default=None)
    if blocked:
        print(f"        O Google bloqueou temporariamente este IP por excesso de pedidos. Tente de novo depois das "
              f"{time.strftime('%H:%M', time.localtime(blocked))}; enquanto isso o app usa a reserva "
              "e depois volta ao Google sozinho.")
    results += [check(name, fn) for name, fn in RESERVE.items()]
    ok = sum(good for good, _ in results)
    if url:
        vid = extract_video_id(url)
        try:
            tracks = list(transcript_api().list(vid))
            names = ", ".join(f"{t.language_code}{'(auto)' if t.is_generated else ''}" for t in tracks)
            track = pick_transcript(tracks, "auto")
            n = len(list(track.fetch()))
            print(f"[OK]    Legendas do YouTube: {n} falas em {track.language_code} (disponíveis: {names})")
        except Exception as e:
            print(f"[FALHA] Legendas do YouTube: {friendly_error(e)} [{type(e).__name__}]")
    daily = f"{MYMEMORY_DAILY_CHARS:,}".replace(",", ".")
    print("\nPelo menos um tradutor precisa estar OK para o app traduzir. O MyMemory é só a reserva: "
          f"até {daily} caracteres por dia sem cadastro (umas 200 falas), usados quando o Google falha.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Servidor local (o player do YouTube exige página servida por http, não file://)
# --------------------------------------------------------------------------- #
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start_server() -> int:
    handler = functools.partial(QuietHandler, directory=str(UI_DIR))
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PREFERRED_PORT), handler)
    except OSError:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd.server_port


def main():
    args = sys.argv[1:]
    log_path = setup_logging()
    if args and args[0] in ("--diagnostico", "--diagnóstico", "--diag"):
        sys.exit(diagnose(args[1] if len(args) > 1 else None))

    import webview  # importado aqui para o diagnóstico rodar mesmo sem o pywebview

    port = start_server()
    url = f"http://127.0.0.1:{port}/index.html"
    if args:
        url += "?u=" + quote(args[0], safe="")

    api = Api(log_path)
    window = webview.create_window(
        "Legendas YT — PT ⇄ EN",
        url,
        js_api=api,
        width=1280,
        height=780,
        min_size=(720, 460),
        background_color="#0f0f10",
    )
    api._window = window
    webview.start()


if __name__ == "__main__":
    main()
