"""
Galeria do Mini App: quem comprou vê o conteúdo dentro da loja, num carrossel estilo Instagram.

Rotas:
  GET /api/loja/conteudo/{item_id}   lista das mídias de um pack/combo COMPRADO (com links assinados)
  GET /api/loja/midia/{item_id}/{n}  a mídia em si (foto com marca d'água do comprador, vídeo com Range)

Proteções (a página não tem o protect_content do Telegram, então compensamos):
  - Só quem comprou recebe a lista; cada link é assinado (HMAC) para AQUELE usuário e expira em 15 min.
  - Fotos e vídeos levam uma marca d'água discreta com o @ e o ID do comprador (vazou, dá para saber quem foi).
    No vídeo a marca é gravada nos quadros (FFmpeg), não é uma camada por cima: baixar o arquivo não tira.
    Cada vídeo marcado fica num cache em disco por comprador; o primeiro play espera a marcação
    (a loja já começa a marcar quando o fã abre o pack).
  - Nada fica em cache no navegador e a página bloqueia o menu de "salvar imagem".
  - Print de tela continua possível, igual em qualquer app.
Limite: o bot só baixa arquivos de até 20 MB pela Bot API. Vídeos maiores aparecem com o botão
"Assistir no chat", que manda o vídeo protegido no privado.
"""
import asyncio
import hashlib
import hmac
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiohttp import web
from PIL import Image, ImageDraw, ImageFont

from config import Config
from database import Database

log = logging.getLogger(__name__)
VALIDADE_LINK = 15 * 60
LIMITE_BOT_API = 20 * 1024 * 1024


class _CacheLRU:
    """Cache em memória limitado por tamanho total (para não estourar a RAM do servidor)."""

    def __init__(self, max_bytes: int):
        self.max_bytes, self.total, self.dados = max_bytes, 0, OrderedDict()

    def get(self, chave):
        if chave in self.dados:
            self.dados.move_to_end(chave)
            return self.dados[chave]
        return None

    def put(self, chave, valor: bytes):
        if len(valor) > self.max_bytes // 2:
            return
        if chave in self.dados:                       # substituindo: desconta o tamanho antigo
            self.total -= len(self.dados.pop(chave))
        self.dados[chave] = valor
        self.total += len(valor)
        while self.total > self.max_bytes:
            _, velho = self.dados.popitem(last=False)
            self.total -= len(velho)


_originais = _CacheLRU(64 * 1024 * 1024)      # arquivos baixados do Telegram
_marcadas = _CacheLRU(32 * 1024 * 1024)        # fotos já com a marca d'água de cada comprador


def _assinar(cfg: Config, user_id: int, item_id: str, n: int, exp: int) -> str:
    chave = hashlib.sha256(b"galeria:" + cfg.bot_token.encode()).digest()
    return hmac.new(chave, f"{user_id}:{item_id}:{n}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


async def _midias_compradas(db: Database, user_id: int, item_id: str, admin: bool = False) -> list[dict] | None:
    """Mídias originais do item, ou None se o usuário não comprou tudo (a criadora vê tudo)."""
    packs = await db.packs_do_item(item_id)
    if not packs or (not admin and not await db.tem_todos(user_id, [p.id for p in packs])):
        return None
    return [m for p in packs for m in p.originais]


async def _baixar(bot: Bot, file_id: str) -> bytes:
    dados = _originais.get(file_id)
    if dados is None:
        buf = BytesIO()
        await bot.download(file_id, destination=buf)
        dados = buf.getvalue()
        _originais.put(file_id, dados)
    return dados


def _camada_marca(tamanho: tuple[int, int], texto: str, alfa: int = 34) -> Image.Image:
    """Camada transparente com o texto repetido em fileiras alternadas (mesmo desenho em fotos e vídeos)."""
    largura, altura = tamanho
    camada = Image.new("RGBA", tamanho, (0, 0, 0, 0))
    lado = max(14, largura // 34)
    try:
        fonte = ImageFont.truetype("DejaVuSans-Bold.ttf", lado)
    except OSError:
        fonte = ImageFont.load_default(size=lado)     # Pillow >= 10.1 traz uma fonte embutida
    d = ImageDraw.Draw(camada)
    passo_x = int(d.textlength(texto, font=fonte)) + lado * 4      # espaço pelo tamanho real do texto
    passo_y = lado * 7
    for linha, y in enumerate(range(-passo_y, altura + passo_y, passo_y)):
        for x in range(-passo_x + (linha % 2) * passo_x // 2, largura + passo_x, passo_x):
            d.text((x, y), texto, font=fonte, fill=(255, 255, 255, alfa))
    return camada


def _marca_dagua(dados: bytes, texto: str) -> bytes:
    img = Image.open(BytesIO(dados)).convert("RGB")
    img.thumbnail((1600, 1600))
    img = Image.alpha_composite(img.convert("RGBA"), _camada_marca(img.size, texto)).convert("RGB")
    saida = BytesIO()
    img.save(saida, "JPEG", quality=88)
    return saida.getvalue()


# ── Vídeo com marca d'água gravada ─────────────────────────────────────────
DIR_VIDEOS = Path(tempfile.gettempdir()) / "videos-marcados"
LIMITE_DISCO_VIDEOS = 1536 * 1024 * 1024        # cache em disco; os mais antigos saem primeiro
LADO_VIDEO = 1280
_marcando: dict[str, asyncio.Task] = {}
_vez_ffmpeg: asyncio.Semaphore | None = None     # um vídeo por vez: o servidor tem 1 GB de RAM


def _texto_marca(username: str | None, uid: int) -> str:
    return (f"@{username} · " if username else "") + f"ID {uid}"


def _caminho_marcado(uid: int, file_id: str, texto: str) -> Path:
    nome = hashlib.sha256(f"{uid}:{file_id}:{texto}".encode()).hexdigest()[:32]
    return DIR_VIDEOS / f"{nome}.mp4"


def _podar_disco() -> None:
    try:
        arquivos = sorted(DIR_VIDEOS.glob("*.mp4"), key=lambda a: a.stat().st_mtime)
        total = sum(a.stat().st_size for a in arquivos)
        while arquivos and total > LIMITE_DISCO_VIDEOS:
            velho = arquivos.pop(0)
            total -= velho.stat().st_size
            velho.unlink(missing_ok=True)
    except OSError:
        log.warning("Não consegui limpar o cache de vídeos marcados", exc_info=True)


def _gravar_marca_video(dados: bytes, destino: Path, texto: str) -> bool:
    """Grava a marca d'água nos quadros do vídeo (H.264, até 1280px). True se deu certo."""
    from blur_service import _frame_ffmpeg
    frame = _frame_ffmpeg(dados)                      # tamanho já na orientação certa (rotação do celular)
    if not frame:
        return False
    w, h = Image.open(BytesIO(frame)).size
    escala = min(1.0, LADO_VIDEO / max(w, h))
    w, h = max(2, int(w * escala) // 2 * 2), max(2, int(h * escala) // 2 * 2)
    DIR_VIDEOS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        entrada, marca, saida = Path(tmp, "in.mp4"), Path(tmp, "marca.png"), Path(tmp, "out.mp4")
        entrada.write_bytes(dados)
        _camada_marca((w, h), texto, alfa=40).save(marca)
        for audio in (["-c:a", "copy"], ["-c:a", "aac", "-b:a", "128k"]):   # copiar o áudio é mais rápido
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(entrada), "-i", str(marca),
                 "-filter_complex", f"[0:v]scale={w}:{h},setsar=1[v];[v][1:v]overlay=0:0,format=yuv420p[s]",
                 "-map", "[s]", "-map", "0:a?", "-threads", "2", "-c:v", "libx264", "-preset", "superfast",
                 "-crf", "23", *audio, "-movflags", "+faststart", str(saida)],
                capture_output=True, timeout=900,
            )
            if proc.returncode == 0 and saida.exists() and saida.stat().st_size > 0:
                temp = destino.with_suffix(".parcial")
                shutil.move(str(saida), temp)
                os.replace(temp, destino)             # só aparece no cache quando está completo
                return True
            log.warning("ffmpeg falhou ao marcar vídeo (%s): %s", audio[1], proc.stderr[-400:])
    return False


async def video_marcado(bot: Bot, file_id: str, uid: int, texto: str) -> Path | None:
    """Caminho do vídeo com a marca deste comprador (marca na hora se ainda não existe)."""
    global _vez_ffmpeg
    destino = _caminho_marcado(uid, file_id, texto)
    if destino.exists():
        os.utime(destino)                             # usado agora: fica mais tempo no cache
        return destino
    if not shutil.which("ffmpeg"):
        return None
    chave = destino.name
    tarefa = _marcando.get(chave)
    if tarefa is None:
        async def marcar() -> Path | None:
            global _vez_ffmpeg
            if _vez_ffmpeg is None:
                _vez_ffmpeg = asyncio.Semaphore(1)
            try:
                async with _vez_ffmpeg:
                    if destino.exists():
                        return destino
                    dados = await _baixar(bot, file_id)
                    ok = await asyncio.to_thread(_gravar_marca_video, dados, destino, texto)
                    if ok:
                        await asyncio.to_thread(_podar_disco)
                        return destino
                    return None
            except Exception:
                log.exception("Não consegui marcar o vídeo %s", file_id)
                return None
            finally:
                _marcando.pop(chave, None)
        tarefa = _marcando[chave] = asyncio.create_task(marcar())
    return await asyncio.shield(tarefa)               # o fã fechar a galeria não cancela a marcação


async def _texto_do_usuario(db: Database, uid: int) -> str:
    usuario = await db._um("SELECT username FROM usuarios WHERE user_id = ?", (uid,))
    return _texto_marca(usuario["username"] if usuario else None, uid)


def _resposta_com_range(request: web.Request, dados: bytes, tipo: str) -> web.Response:
    """iPhone só toca <video> se o servidor responder a pedidos parciais (Range)."""
    cab = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store", "Content-Type": tipo}
    faixa = request.headers.get("Range", "")
    total = len(dados)
    if faixa.startswith("bytes="):
        ini, _, fim = faixa[6:].split(",")[0].partition("-")
        try:
            if ini:
                a, b = int(ini), int(fim) if fim else total - 1
            else:
                a, b = max(0, total - int(fim)), total - 1
        except ValueError:
            a, b = 0, total - 1
        b = min(b, total - 1)
        if a > b:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{total}"})
        cab["Content-Range"] = f"bytes {a}-{b}/{total}"
        return web.Response(status=206, body=dados[a:b + 1], headers=cab)
    return web.Response(body=dados, headers=cab)


# ════════════════════════════════════════════════════════════════════════════
async def conteudo(request: web.Request) -> web.Response:
    from loja import _usuario           # evita import circular

    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    user = _usuario(request)
    item_id = request.match_info["item_id"]
    midias = await _midias_compradas(db, user["id"], item_id, user["id"] in cfg.admin_ids)
    if midias is None:
        raise web.HTTPForbidden(text='{"erro":"nao_comprado"}', content_type="application/json")

    exp = int(time.time()) + VALIDADE_LINK
    lista = []
    for n, m in enumerate(midias):
        grande = False
        if m["tipo"] == "video" and _originais.get(m["file_id"]) is None:
            try:
                arq = await bot.get_file(m["file_id"])
                grande = (arq.file_size or 0) > LIMITE_BOT_API
            except Exception:                                   # "file is too big" e afins
                grande = True
        s = _assinar(cfg, user["id"], item_id, n, exp)
        lista.append({"tipo": m["tipo"], "grande": grande,
                      "url": f"/api/loja/midia/{item_id}/{n}?u={user['id']}&e={exp}&s={s}"})
    if await db.get_ajuste("marca_dagua", "1") == "1":   # já começa a marcar os vídeos (um de cada vez)
        texto = await _texto_do_usuario(db, user["id"])
        for m, x in zip(midias, lista):
            if m["tipo"] == "video" and not x["grande"]:
                asyncio.create_task(video_marcado(bot, m["file_id"], user["id"], texto))
    item = await db.get_item(item_id)
    return web.json_response({"titulo": item.titulo if item else "", "midias": lista},
                             headers={"Cache-Control": "no-store"})


async def midia(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    item_id, n = request.match_info["item_id"], int(request.match_info["n"])
    try:
        uid, exp, sig = int(request.query["u"]), int(request.query["e"]), request.query["s"]
    except (KeyError, ValueError):
        raise web.HTTPForbidden()
    if exp < time.time() or not hmac.compare_digest(sig, _assinar(cfg, uid, item_id, n, exp)):
        raise web.HTTPForbidden()
    if uid not in cfg.admin_ids and await db.esta_banido(uid):
        raise web.HTTPForbidden()
    midias = await _midias_compradas(db, uid, item_id, uid in cfg.admin_ids)
    if midias is None or not 0 <= n < len(midias):
        raise web.HTTPForbidden()
    m = midias[n]

    marca = await db.get_ajuste("marca_dagua", "1") == "1"      # a criadora pode desligar a marca
    if m["tipo"] == "video" and marca:
        caminho = await video_marcado(bot, m["file_id"], uid, await _texto_do_usuario(db, uid))
        if caminho:
            return web.FileResponse(caminho, headers={"Cache-Control": "private, no-store",
                                                      "Content-Type": "video/mp4"})
        log.warning("Galeria: vídeo %s saiu sem marca (FFmpeg indisponível ou falhou)", m["file_id"])

    try:
        dados = await _baixar(bot, m["file_id"])
    except Exception:
        log.exception("Galeria: não consegui baixar %s", m["file_id"])
        raise web.HTTPRequestEntityTooLarge(max_size=LIMITE_BOT_API, actual_size=0)

    if m["tipo"] == "photo" and not marca:
        return web.Response(body=dados, content_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    if m["tipo"] == "photo":
        chave = (uid, m["file_id"])
        marcada = _marcadas.get(chave)
        if marcada is None:
            marcada = await asyncio.to_thread(_marca_dagua, dados, await _texto_do_usuario(db, uid))
            _marcadas.put(chave, marcada)
        return web.Response(body=marcada, content_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    return _resposta_com_range(request, dados, "video/mp4")


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/api/loja/conteudo/{item_id}", conteudo)
    app.router.add_get("/api/loja/midia/{item_id}/{n:\\d+}", midia)
