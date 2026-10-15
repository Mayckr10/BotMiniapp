"""
Mini App da loja (Telegram WebApp): a vitrine abre DENTRO do Telegram, o cliente paga o PIX
na própria página e o bot entrega o conteúdo no chat privado, igual à compra pelo bot.

Rotas (no mesmo servidor aiohttp do webhook, então usam o mesmo HTTPS):
  GET  /loja                       página do Mini App (miniapp/index.html)
  GET  /api/loja/dados             perfil, planos, packs, combos e compras do cliente
  GET  /api/loja/previa/{item_id}  imagem EMBAÇADA do item (nunca a original)
  GET  /api/loja/foto              foto de perfil da criadora
  POST /api/loja/idade             confirmação 18+
  POST /api/loja/comprar           cria o pedido e devolve o PIX (QR + copia e cola)
  GET  /api/loja/pedido/{pix_id}   status do pagamento (?verificar=1 consulta o gateway, "Já paguei")
  POST /api/loja/reenviar          reenvia no chat um pack já comprado

Segurança: toda chamada da API manda o `initData` do Telegram no header X-Telegram-Init-Data.
O servidor confere a assinatura HMAC com o token do bot (aiogram.utils.web_app), então sabe
QUEM está comprando sem login, e ninguém consegue se passar por outro usuário.
As mídias originais nunca passam pela página: só o bot as envia, com protect_content.
"""
import base64
import logging
import time
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

from assinaturas import data_local
from config import Config
from database import Database, Pack, fmt_reais
from payments import MockPixGateway
from vendas import criar_pedido
from webhook import entregar_pack, entregar_packs, processar_pagamento

log = logging.getLogger(__name__)
PAGINA = Path(__file__).parent / "miniapp" / "index.html"
VALIDADE_INIT_DATA = 24 * 3600
_cache_imagens: dict[str, bytes] = {}


def _resumo(packs: list[Pack]) -> str:
    fotos = sum(m["tipo"] == "photo" for p in packs for m in p.originais)
    videos = sum(m["tipo"] == "video" for p in packs for m in p.originais)
    partes = ([f"{fotos} foto{'s' if fotos != 1 else ''}"] if fotos else []) + \
             ([f"{videos} vídeo{'s' if videos != 1 else ''}"] if videos else [])
    return " · ".join(partes)


def _usuario(request: web.Request) -> dict:
    """Valida o initData e devolve o usuário do Telegram. 401 se for inválido ou velho."""
    cfg: Config = request.app["cfg"]
    bruto = request.headers.get("X-Telegram-Init-Data", "")
    try:
        dados = safe_parse_webapp_init_data(cfg.bot_token, bruto)
    except ValueError:
        raise web.HTTPUnauthorized(text='{"erro":"init_data_invalido"}', content_type="application/json")
    if not dados.user or time.time() - dados.auth_date.timestamp() > VALIDADE_INIT_DATA:
        raise web.HTTPUnauthorized(text='{"erro":"init_data_expirado"}', content_type="application/json")
    return {"id": dados.user.id, "nome": dados.user.first_name, "username": dados.user.username}


async def _idade_ok(db: Database, cfg: Config, user_id: int) -> str | None:
    if not cfg.exigir_maioridade or user_id in cfg.admin_ids:
        return "ok"
    return await db.status_idade(user_id)


async def _imagem(bot: Bot, file_id: str) -> bytes:
    if file_id not in _cache_imagens:
        buf = BytesIO()
        await bot.download(file_id, destination=buf)
        _cache_imagens[file_id] = buf.getvalue()
    return _cache_imagens[file_id]


# ════════════════════════════════════════════════════════════════════════════
async def pagina(_: web.Request) -> web.FileResponse:
    return web.FileResponse(PAGINA, headers={"Cache-Control": "no-cache"})


async def dados(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    user = _usuario(request)
    await db.registrar_usuario(user["id"], user["nome"], user["username"])

    planos = await db.listar_planos()
    mensal = next((p for p in planos if p.dias == 30), None)

    def economia(p):
        if not mensal or p.dias <= 30:
            return None
        cheio = mensal.preco * p.dias // 30
        return round(100 - p.preco * 100 / cheio) if cheio > p.preco else None

    packs = []
    for p in await db.listar_packs():
        packs.append({"id": p.id, "titulo": p.titulo, "preco_fmt": p.preco_fmt, "preco": p.preco,
                      "resumo": _resumo([p]), "comprado": await db.ja_comprou(user["id"], p.id),
                      "fotos": sum(m["tipo"] == "photo" for m in p.originais),
                      "videos": sum(m["tipo"] == "video" for m in p.originais),
                      "criado_em": p.criado_em})
    todos = await db.listar_packs(tipo=None)
    totais = {"conteudos": len(todos),
              "fotos": sum(m["tipo"] == "photo" for p in todos for m in p.originais),
              "videos": sum(m["tipo"] == "video" for p in todos for m in p.originais)}
    combos = []
    for c in await db.listar_combos():
        itens = await db.packs_do_item(c.id)
        combos.append({"id": c.id, "titulo": c.titulo, "preco_fmt": c.preco_fmt, "preco": c.preco,
                       "resumo": f"{len(itens)} packs · {_resumo(itens)}", "packs": [p.titulo for p in itens],
                       "separados_fmt": fmt_reais(sum(p.preco for p in itens)),
                       "comprado": await db.tem_todos(user["id"], [p.id for p in itens])})
    assinatura = await db.get_assinatura(user["id"])
    return web.json_response({
        "criadora": {"nome": cfg.criadora_nome,
                     "bio": await db.get_ajuste("bio", "Conteúdo exclusivo todos os dias 🔥"),
                     "tem_foto": bool(await db.get_ajuste("foto_perfil")),
                     "totais": totais},
        "usuario": {"nome": user["nome"]},
        "idade": await _idade_ok(db, cfg, user["id"]),
        "teste": isinstance(request.app["gateway"], MockPixGateway),
        "assinatura": {"ativa": True, "ate": data_local(assinatura.expira_em, cfg)}
                      if assinatura and assinatura.ativa else None,
        "planos": [{"id": p.item_id, "nome": p.nome, "dias": p.dias, "preco_fmt": p.preco_fmt,
                    "economia": economia(p)} for p in planos],
        "packs": packs,
        "combos": combos,
        "gorjetas": [int(v) * 100 for v in cfg.gorjeta_valores.split(",") if v.strip().isdigit()],
        "minhas": [{"id": p.id, "titulo": p.titulo} for p in await db.packs_do_cliente(user["id"])],
    })


async def previa(request: web.Request) -> web.Response:
    """Só a amostra embaçada: o arquivo original nunca é servido aqui."""
    db: Database = request.app["db"]
    packs = await db.packs_do_item(request.match_info["item_id"])
    capa = next((p.blur[0]["file_id"] for p in packs if p.blur), None)
    if not capa:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], capa), content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


async def foto(request: web.Request) -> web.Response:
    file_id = await request.app["db"].get_ajuste("foto_perfil")
    if not file_id:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], file_id), content_type="image/jpeg")


async def idade(request: web.Request) -> web.Response:
    user = _usuario(request)
    corpo = await request.json()
    await request.app["db"].registrar_idade(user["id"], maior=bool(corpo.get("maior")))
    return web.json_response({"ok": True, "idade": "ok" if corpo.get("maior") else "menor"})


async def comprar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    user = _usuario(request)
    corpo = await request.json()
    status_idade = await _idade_ok(db, cfg, user["id"])
    if status_idade != "ok":
        return web.json_response({"status": "idade", "idade": status_idade}, status=403)

    pedido = await criar_pedido(db, request.app["gateway"], cfg, user["id"], str(corpo.get("item_id", "")),
                                (corpo.get("cupom") or "").strip() or None)
    if pedido.status == "inexistente":
        return web.json_response({"status": "inexistente"}, status=404)
    if pedido.status == "ja_comprou":
        await entregar_packs(bot, user["id"], pedido.packs, pedido.item.titulo)
        return web.json_response({"status": "ja_comprou"})
    if pedido.status == "erro_gateway":
        return web.json_response({"status": "erro_gateway"}, status=502)

    c = pedido.cobranca
    return web.json_response({
        "status": "ok",
        "titulo": pedido.item.titulo,
        "pix_id": c.pix_id,
        "copia_e_cola": c.copia_e_cola,
        "qr": "data:image/png;base64," + base64.b64encode(c.qr_png).decode(),
        "valor_fmt": fmt_reais(pedido.preco),
        "original_fmt": pedido.item.preco_fmt if pedido.preco != pedido.item.preco else None,
        "cupom": {"codigo": pedido.cupom.codigo, "descricao": pedido.cupom.descricao} if pedido.cupom else None,
        "cupom_erro": f"Cupom {pedido.cupom_invalido} não aplicado: {pedido.motivo_cupom}"
                      if pedido.cupom_invalido else None,
        "expira": pedido.expira.isoformat(),
    })


async def pedido(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    pix_id = request.match_info["pix_id"]
    venda = await db.get_venda_por_pix(pix_id)
    if not venda or venda.user_id != user["id"]:
        raise web.HTTPNotFound()
    if venda.status != "PAGO" and request.query.get("verificar"):
        await processar_pagamento(request.app["bot"], db, request.app["gateway"], pix_id, request.app["cfg"])
        venda = await db.get_venda_por_pix(pix_id)
    return web.json_response({"status": venda.status})


async def teste_pagar(request: web.Request) -> web.Response:
    """Só com PIX_GATEWAY=mock: simula o pagamento pela loja (testar pelo celular)."""
    gw = request.app["gateway"]
    user = _usuario(request)
    pix_id = str((await request.json()).get("pix_id", ""))
    venda = await request.app["db"].get_venda_por_pix(pix_id)
    if not isinstance(gw, MockPixGateway) or not venda or venda.user_id != user["id"] \
            or not gw.marcar_pago_teste(pix_id):
        raise web.HTTPNotFound()
    await processar_pagamento(request.app["bot"], request.app["db"], gw, pix_id, request.app["cfg"])
    return web.json_response({"ok": True})


async def reenviar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    pack_id = str((await request.json()).get("pack_id", ""))
    pack = await db.get_pack(pack_id)
    if not pack or not await db.ja_comprou(user["id"], pack_id):
        raise web.HTTPForbidden()
    await entregar_pack(request.app["bot"], user["id"], pack)
    return web.json_response({"ok": True})


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/loja", pagina)
    app.router.add_get("/api/loja/dados", dados)
    app.router.add_get("/api/loja/previa/{item_id}", previa)
    app.router.add_get("/api/loja/foto", foto)
    app.router.add_post("/api/loja/idade", idade)
    app.router.add_post("/api/loja/comprar", comprar)
    app.router.add_get("/api/loja/pedido/{pix_id}", pedido)
    app.router.add_post("/api/loja/reenviar", reenviar)
    app.router.add_post("/api/loja/teste_pagar", teste_pagar)


def url_loja(cfg: Config) -> str | None:
    """O Telegram só abre Mini Apps em HTTPS (o simulador aceita http)."""
    base = cfg.public_base_url.rstrip("/")
    if cfg.loja_ativa and (base.startswith("https://") or cfg.telegram_api_base):
        return f"{base}/loja"
    return None

