"""
Galeria do Mini App: quem comprou vê o conteúdo dentro da loja, num carrossel estilo Instagram.

Rotas:
  GET /api/loja/conteudo/{item_id}   lista das mídias de um pack/combo COMPRADO (com links assinados)
  GET /api/loja/midia/{item_id}/{n}  a mídia em si (foto com marca d'água do comprador, vídeo com Range)

Proteções (a página não tem o protect_content do Telegram, então compensamos):
  - Só quem comprou recebe a lista; cada link é assinado (HMAC) para AQUELE usuário e expira em 15 min.
  - Fotos levam uma marca d'água discreta com o @ e o ID do comprador (vazou, dá para saber quem foi).
  - Nada fica em cache no navegador e a página bloqueia o menu de "salvar imagem".
  - Print de tela continua possível, igual em qualquer app.
Limite: o bot só baixa arquivos de até 20 MB pela Bot API. Vídeos maiores aparecem com o botão
"Assistir no chat", que manda o vídeo protegido no privado.
"""
import asyncio
import hashlib
import hmac
import logging
import time
from collections import OrderedDict
from io import BytesIO

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
        self.dados[chave] = valor
        self.total += len(valor)
        while self.total > self.max_bytes:
            _, velho = self.dados.popitem(last=False)
            self.total -= len(velho)


_originais = _CacheLRU(120 * 1024 * 1024)      # arquivos baixados do Telegram
_marcadas = _CacheLRU(60 * 1024 * 1024)        # fotos já com a marca d'água de cada comprador


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


def _marca_dagua(dados: bytes, texto: str) -> bytes:
    img = Image.open(BytesIO(dados)).convert("RGB")
    img.thumbnail((1600, 1600))
    camada = Image.new("RGBA", img.size, (0, 0, 0, 0))
    lado = max(14, img.width // 34)
    try:
        fonte = ImageFont.truetype("DejaVuSans-Bold.ttf", lado)
    except OSError:
        fonte = ImageFont.load_default(size=lado)     # Pillow >= 10.1 traz uma fonte embutida
    d = ImageDraw.Draw(camada)
    passo_x, passo_y = lado * 16, lado * 7
    for linha, y in enumerate(range(-passo_y, img.height + passo_y, passo_y)):
        for x in range(-passo_x + (linha % 2) * passo_x // 2, img.width + passo_x, passo_x):
            d.text((x, y), texto, font=fonte, fill=(255, 255, 255, 34))
    img = Image.alpha_composite(img.convert("RGBA"), camada).convert("RGB")
    saida = BytesIO()
    img.save(saida, "JPEG", quality=88)
    return saida.getvalue()


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

    try:
        dados = await _baixar(bot, m["file_id"])
    except Exception:
        log.exception("Galeria: não consegui baixar %s", m["file_id"])
        raise web.HTTPRequestEntityTooLarge(max_size=LIMITE_BOT_API, actual_size=0)

    if m["tipo"] == "photo" and await db.get_ajuste("marca_dagua", "1") != "1":     # a criadora desligou a marca
        return web.Response(body=dados, content_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    if m["tipo"] == "photo":
        chave = (uid, m["file_id"])
        marcada = _marcadas.get(chave)
        if marcada is None:
            usuario = await db._um("SELECT username FROM usuarios WHERE user_id = ?", (uid,))
            texto = (f"@{usuario['username']} · " if usuario and usuario["username"] else "") + f"ID {uid}"
            marcada = await asyncio.to_thread(_marca_dagua, dados, texto)
            _marcadas.put(chave, marcada)
        return web.Response(body=marcada, content_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    return _resposta_com_range(request, dados, "video/mp4")


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/api/loja/conteudo/{item_id}", conteudo)
    app.router.add_get("/api/loja/midia/{item_id}/{n:\\d+}", midia)
