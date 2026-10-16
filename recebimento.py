"""
Conta de recebimento (Pushin Pay) da criadora e split do dono da plataforma.

  - A criadora cola o token da conta Pushin Pay dela em Ajustes › Vendas › Recebimento. O token é validado
    na Pushin Pay, salvo na tabela ajustes (pushinpay_token) e nunca volta para o app (só os 4 últimos dígitos).
  - O dono da plataforma (DONO_IDS) cadastra a conta dele para receber o split com /split <account_id> <pct>.
    Fica salvo em ajustes (split_conta / split_pct) e vale no lugar de PLATAFORMA_ACCOUNT_ID / PLATAFORMA_PCT.
"""
import html
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiohttp import web

from config import Config
from database import Database
from payments import PushinPayGateway

log = logging.getLogger(__name__)
TOKEN_OK = re.compile(r"^[A-Za-z0-9|._\-]{20,400}$")
CONTA_OK = re.compile(r"^[A-Za-z0-9\-]{8,80}$")


def _dona(request: web.Request) -> dict:
    """Só a criadora dona da loja (não a equipe): é a conta para onde vai o dinheiro."""
    from loja import _usuario
    user = _usuario(request)
    if user["id"] not in request.app["cfg"].admin_ids:
        raise web.HTTPForbidden(text='{"ok":false,"erro":"Só a criadora pode mexer na conta de recebimento."}',
                                content_type="application/json")
    return user


async def _estado(app: web.Application) -> dict:
    db: Database = app["db"]
    cfg: Config = app["cfg"]
    gw = app["gateway"]
    salvo = await db.get_ajuste("pushinpay_token") or ""
    conta, pct = await gw.split()
    return {"ok": True, "conectado": bool(salvo), "final": salvo[-4:] if salvo else "",
            "pelo_servidor": not salvo and bool(cfg.pushinpay_token),
            "em_teste": await gw.em_teste(), "gateway": "Pushin Pay",
            "taxa_pct": pct if conta else 0}


async def ver(request: web.Request) -> web.Response:
    _dona(request)
    return web.json_response(await _estado(request.app))


async def conectar(request: web.Request) -> web.Response:
    user = _dona(request)
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    token = re.sub(r"\s+", "", str(corpo.get("token", "")))
    if token.lower().startswith("bearer"):
        token = token[6:]
    if not TOKEN_OK.match(token):
        return web.json_response({"ok": False, "erro": "Esse token não parece certo. Copie de novo na Pushin Pay."},
                                 status=400)
    gw = PushinPayGateway(request.app["cfg"], token)
    try:
        valido = await gw.token_valido()
    finally:
        await gw.http.aclose()
    if valido is False:
        return web.json_response({"ok": False, "erro": "A Pushin Pay recusou esse token. Confira se copiou inteiro."},
                                 status=400)
    await request.app["db"].set_ajuste("pushinpay_token", token)
    log.info("Conta de recebimento conectada por %s (token ...%s)", user["id"], token[-4:])
    estado = await _estado(request.app)
    if valido is None:
        estado["aviso"] = "Salvei, mas a Pushin Pay não respondeu agora para confirmar. Faça uma compra de teste."
    return web.json_response(estado)


async def desconectar(request: web.Request) -> web.Response:
    user = _dona(request)
    db: Database = request.app["db"]
    await db.conn.execute("DELETE FROM ajustes WHERE chave = 'pushinpay_token'")
    await db.conn.commit()
    log.info("Conta de recebimento desconectada por %s", user["id"])
    return web.json_response(await _estado(request.app))


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/api/criadora/recebimento", ver)
    app.router.add_post("/api/criadora/recebimento", conectar)
    app.router.add_post("/api/criadora/recebimento/desconectar", desconectar)


# ════════════════════════════════════════════════════════════════════════════
#  Bot: /split (dono da plataforma)
# ════════════════════════════════════════════════════════════════════════════
def get_router(cfg: Config, db: Database, gateway) -> Router:
    router = Router(name="recebimento")
    router.message.filter(F.chat.type == "private")

    @router.message(Command("split"), F.from_user.id.in_(cfg.dono_ids))
    async def split(msg: Message, command: CommandObject):
        args = (command.args or "").split()
        if args and args[0].lower() in ("off", "desligar", "0"):
            await db.set_ajuste("split_conta", "")
            await db.set_ajuste("split_pct", "0")
            await msg.answer("✂️ Split desligado. As vendas vão 100% para a criadora.")
            return
        if args:
            conta = args[0]
            try:
                pct = int(args[1].replace("%", "")) if len(args) > 1 else 10
            except ValueError:
                pct = -1
            if not CONTA_OK.match(conta) or not 1 <= pct <= 50:
                await msg.answer("Uso: <code>/split ID_DA_CONTA 10</code>\n"
                                 "O ID é o da sua conta Pushin Pay (em Configurações › Conta). "
                                 "A % vai de 1 a 50 (limite da Pushin Pay).")
                return
            await db.set_ajuste("split_conta", conta)
            await db.set_ajuste("split_pct", str(pct))
            log.info("Split configurado pelo dono %s: %s%%", msg.from_user.id, pct)
        conta, pct = await gateway.split()
        conectada = bool(await gateway.token())
        if conta and pct:
            texto = (f"✂️ <b>Split ativo</b>\nConta: <code>{html.escape(conta)}</code>\n"
                     f"Você recebe {pct}% de cada venda, direto na sua conta Pushin Pay.")
        else:
            texto = "✂️ <b>Split desligado.</b>"
        texto += ("\n\nConta da criadora: " + ("✅ conectada" if conectada else "⚠️ ainda não conectada (modo teste)")
                  + "\n\n<code>/split ID 10</code> muda · <code>/split off</code> desliga")
        await msg.answer(texto)

    return router
