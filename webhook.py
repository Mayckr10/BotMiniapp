"""
Servidor HTTP (aiohttp) que recebe o webhook do gateway PIX e entrega o conteúdo.

Rotas:
  POST /webhook/pix          -> chamada pelo gateway quando o PIX é pago
  GET  /health               -> checagem simples
  (somente com PIX_GATEWAY=mock)
  GET  /mock/cobrancas       -> lista cobranças do mock
  POST /mock/pagar/{pix_id}  -> simula o pagamento: o "gateway" chama o /webhook/pix

Segurança do webhook:
  1. Segredo compartilhado (header X-Webhook-Secret ou ?token=) -> barra chamadas aleatórias.
  2. Revalidação: o status é sempre reconsultado na API do gateway antes de entregar.
  3. Idempotência: marcar_pago() só retorna True uma vez -> nunca entrega em dobro.
"""
import hmac
import html
import logging

import httpx
from aiogram import Bot
from aiogram.types import InputMediaPhoto, InputMediaVideo
from aiohttp import web

from config import Config
from assinaturas import ativar_assinatura
from database import Database, Pack, fmt_reais
from payments import MockPixGateway, PixGateway

log = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════════
#  Entrega
# ════════════════════════════════════════════════════════════════════════════
async def entregar_pack(bot: Bot, user_id: int, pack: Pack, agradecer: bool = True) -> None:
    """Envia as mídias ORIGINAIS com protect_content=True (sem encaminhar/salvar)."""
    midias = pack.originais
    for i in range(0, len(midias), 10):                      # limite de 10 por álbum
        lote = midias[i:i + 10]
        if len(lote) == 1:
            m = lote[0]
            enviar = bot.send_photo if m["tipo"] == "photo" else bot.send_video
            await enviar(user_id, m["file_id"], protect_content=True)
        else:
            await bot.send_media_group(
                user_id,
                [InputMediaPhoto(media=m["file_id"]) if m["tipo"] == "photo"
                 else InputMediaVideo(media=m["file_id"]) for m in lote],
                protect_content=True,
            )
    if agradecer:
        await bot.send_message(user_id, f"💖 Aproveite <b>{html.escape(pack.titulo)}</b>! Obrigada pela compra.",
                               protect_content=True)


async def entregar_packs(bot: Bot, user_id: int, packs: list[Pack], titulo: str) -> None:
    """Entrega um ou vários packs (combo) e agradece uma única vez no final."""
    for pack in packs:
        if len(packs) > 1:
            await bot.send_message(user_id, f"📦 <b>{html.escape(pack.titulo)}</b>", protect_content=True)
        await entregar_pack(bot, user_id, pack, agradecer=False)
    await bot.send_message(user_id, f"💖 Aproveite <b>{html.escape(titulo)}</b>! Obrigada pela compra.\n"
                                    "Use /meus_packs para receber de novo quando quiser.", protect_content=True)


async def processar_pagamento(bot: Bot, db: Database, gateway: PixGateway, pix_id: str,
                              cfg: Config | None = None) -> str:
    """
    Confirma e entrega uma venda. Retorna:
      'desconhecido' | 'nao_pago' | 'ja_processado' | 'entregue'
    Usado tanto pelo webhook quanto pelo botão "Já paguei".
    """
    venda = await db.get_venda_por_pix(pix_id)
    if not venda:
        return "desconhecido"
    if not await gateway.consultar_pago(pix_id):           # nunca confia só no payload
        return "nao_pago"
    if not await db.marcar_pago(pix_id):                    # já estava PAGO
        return "ja_processado"

    log.info("Venda %s paga (pix %s): %s para %s", venda.id, pix_id, venda.pack_id, venda.user_id)

    # Assinatura VIP
    if venda.pack_id.startswith("plano_"):
        plano = await db.get_plano(int(venda.pack_id[6:]), incluir_inativos=True)
        await bot.send_message(venda.user_id, "✅ <b>Pagamento confirmado!</b>")
        await ativar_assinatura(bot, db, cfg, venda, plano)
        return "entregue"

    # Gorjeta
    if venda.pack_id.startswith("gorjeta_"):
        await bot.send_message(venda.user_id, f"💝 Recebi sua gorjeta de <b>{fmt_reais(venda.valor)}</b>. "
                                              "Muito obrigada pelo carinho! 🥰")
        for admin_id in cfg.admin_ids:
            try:
                aviso = await bot.send_message(admin_id, f"💝 Gorjeta de {fmt_reais(venda.valor)} de "
                                                         f"<code>{venda.user_id}</code>.\n"
                                                         "Responda esta mensagem para agradecer.")
                await db.salvar_relay(admin_id, aviso.message_id, venda.user_id)
            except Exception:
                pass
        return "entregue"

    # Pack, combo ou PPV
    item = await db.get_item(venda.pack_id)
    packs = await db.packs_do_item(venda.pack_id)
    await db.liberar_acesso(venda.user_id, [p.id for p in packs], venda.id)
    await bot.send_message(venda.user_id, "✅ <b>Pagamento confirmado!</b> Enviando seu conteúdo…")
    await entregar_packs(bot, venda.user_id, packs, item.titulo if item else "seu conteúdo")
    return "entregue"


# ════════════════════════════════════════════════════════════════════════════
#  Rotas HTTP
# ════════════════════════════════════════════════════════════════════════════
async def _ler_corpo(request: web.Request) -> dict:
    if request.content_type == "application/json":
        return await request.json()
    return dict(await request.post())                       # alguns gateways mandam form-urlencoded


async def webhook_pix(request: web.Request) -> web.Response:
    app = request.app
    cfg: Config = app["cfg"]

    segredo = request.headers.get("X-Webhook-Secret") or request.query.get("token", "")
    if not hmac.compare_digest(segredo, cfg.webhook_secret):
        log.warning("Webhook com segredo inválido de %s", request.remote)
        return web.json_response({"ok": False, "erro": "nao_autorizado"}, status=401)

    try:
        evento = app["gateway"].ler_webhook(await _ler_corpo(request))
    except Exception:
        return web.json_response({"ok": False, "erro": "payload_invalido"}, status=400)

    if not evento.pago:
        return web.json_response({"ok": True, "ignorado": True})

    try:
        resultado = await processar_pagamento(app["bot"], app["db"], app["gateway"], evento.pix_id, app["cfg"])
    except Exception:
        log.exception("Erro ao processar pagamento %s", evento.pix_id)
        # 500 faz o gateway reenviar depois; a idempotência evita entrega duplicada
        return web.json_response({"ok": False}, status=500)
    return web.json_response({"ok": True, "resultado": resultado})


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True})


# ── Rotas do mock (simulam o lado do gateway) ──────────────────────────────
async def mock_listar(request: web.Request) -> web.Response:
    gw: MockPixGateway = request.app["gateway"]
    return web.json_response(list(gw.cobrancas.values()))


async def mock_pagar(request: web.Request) -> web.Response:
    gw: MockPixGateway = request.app["gateway"]
    cfg: Config = request.app["cfg"]
    pix_id = request.match_info["pix_id"]
    if pix_id not in gw.cobrancas:
        return web.json_response({"ok": False, "erro": "cobranca_inexistente"}, status=404)
    gw.cobrancas[pix_id]["status"] = "paid"
    # Igual a um gateway de verdade: dispara um POST para o nosso webhook
    url = f"http://127.0.0.1:{cfg.webhook_port}/webhook/pix"
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(url, json={"id": pix_id, "status": "paid", "value": gw.cobrancas[pix_id]["valor"]},
                            headers={"X-Webhook-Secret": cfg.webhook_secret})
    return web.json_response({"ok": True, "webhook_status": r.status_code, "webhook_resposta": r.json()})


def criar_app_web(bot: Bot, db: Database, gateway: PixGateway, cfg: Config) -> web.Application:
    app = web.Application()
    app["bot"], app["db"], app["gateway"], app["cfg"] = bot, db, gateway, cfg
    app.router.add_post("/webhook/pix", webhook_pix)
    app.router.add_get("/health", health)
    if isinstance(gateway, MockPixGateway):
        app.router.add_get("/mock/cobrancas", mock_listar)
        app.router.add_post("/mock/pagar/{pix_id}", mock_pagar)
    return app
