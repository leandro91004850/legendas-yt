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
import socket
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

from tradutor import TranslateError, Translator, google_batch, microsoft_batch

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
        translator = Translator(src_code if src_code in ("en", "pt") else "auto", tgt)
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
    ok = 0
    for name, fn in (("Google", google_batch), ("Microsoft", microsoft_batch)):
        t0 = time.time()
        try:
            out = fn(samples, "en", "pt")
            print(f"[OK]    {name} ({time.time() - t0:.1f} s): {out}")
            ok += 1
        except Exception as e:
            print(f"[FALHA] {e}" if isinstance(e, TranslateError) else f"[FALHA] {name}: {e}")
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
    print("\nPelo menos um tradutor precisa estar OK para o app traduzir.")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Servidor local
# O player do YouTube exige página servida por http (não file://) e aberta por um
# NOME, não por IP: com a página em 127.0.0.1 (ou [::1], 192.168.…) o YouTube
# recusa alguns vídeos (ex.: VVHb0f-nx1Y, uma compilação de músicas) com "Este
# vídeo não está disponível", mesmo com a incorporação liberada pelo dono.
# Por isso a interface abre em http://localhost:PORTA.
# "localhost" pode virar 127.0.0.1 ou ::1, então o servidor escuta nos dois.
# --------------------------------------------------------------------------- #
HOST = "localhost"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class LocalServer(ThreadingHTTPServer):
    # No Windows, o SO_REUSEADDR que o http.server liga deixa outro programa (ex.: outra
    # cópia do app) escutar na mesma porta e responder no lugar deste. Porta exclusiva.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class LocalServer6(LocalServer):
    address_family = socket.AF_INET6


def has_ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def start_server() -> int:
    handler = functools.partial(QuietHandler, directory=str(UI_DIR))
    ipv6 = socket.has_ipv6 and has_ipv6_loopback()
    for port in (PREFERRED_PORT, 0, 0, 0):
        servers = []
        try:
            servers.append(LocalServer(("127.0.0.1", port), handler))
            if ipv6:  # mesma porta no ::1, senão "localhost" pode cair em outro programa
                servers.append(LocalServer6(("::1", servers[0].server_port), handler))
        except OSError:  # porta ocupada: tenta outra
            for s in servers:
                s.server_close()
            continue
        for s in servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        return servers[0].server_port
    raise OSError("Não consegui abrir o servidor local da interface.")


def main():
    args = sys.argv[1:]
    log_path = setup_logging()
    if args and args[0] in ("--diagnostico", "--diagnóstico", "--diag"):
        sys.exit(diagnose(args[1] if len(args) > 1 else None))

    import webview  # importado aqui para o diagnóstico rodar mesmo sem o pywebview

    port = start_server()
    url = f"http://{HOST}:{port}/index.html"
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
