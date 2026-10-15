"""
Proteção da criadora e do conteúdo.

Bloqueio de fãs
  A criadora bloqueia um fã pela loja (Ajustes, ranking, pedidos), pelo aviso de venda no chat
  (botão "🚫 Bloquear fã") ou pelo comando /bloquear ID. Funciona até para quem nunca abriu o bot.
  Quem está bloqueado:
    - não abre a loja (a API responde 403) e o bot não responde nada além do aviso de bloqueio;
    - não consegue comprar nem entrar no canal VIP (o pedido de entrada é recusado);
    - perde a assinatura VIP na hora (é removido do canal);
    - sai das listas de broadcast e PPV.
  /desbloquear ID devolve o acesso (a assinatura removida não volta sozinha).

Limite de reenvios
  Quem comprou pode pedir para receber o pack de novo no chat (/meus_packs, loja, link de compra).
  Sem limite, isso facilita repassar o conteúdo. Agora cada compra tem LIMITE reenvios (padrão 3,
  editável no painel; 0 = sem limite). A entrega original do pagamento não conta, e a galeria da
  loja (com marca d'água) continua liberada depois que o limite acaba.
"""
import html
import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           TelegramObject, WebAppInfo)

from config import Config
from database import Database, Pack

log = logging.getLogger(__name__)
LIMITE_PADRAO = 3
MSG_BANIDO = "🚫 Seu acesso a este perfil foi bloqueado."


# ════════════════════════════════════════════════════════════════════════════
#  Limite de reenvios
# ════════════════════════════════════════════════════════════════════════════
async def limite_reenvio(db: Database) -> int:
    valor = await db.get_ajuste("limite_reenvio", str(LIMITE_PADRAO)) or ""
    return int(valor) if valor.isdigit() else LIMITE_PADRAO


async def reenviar(bot: Bot, db: Database, cfg: Config, user_id: int, packs: list[Pack],
                   titulo: str) -> tuple[bool, int | None]:
    """Entrega de novo no chat, respeitando o limite. Devolve (enviou?, reenvios restantes ou None = sem limite)."""
    from webhook import entregar_packs          # evita import circular

    limite = await limite_reenvio(db)
    ids = [p.id for p in packs]
    if limite == 0 or user_id in cfg.admin_ids:
        await entregar_packs(bot, user_id, packs, titulo)
        return True, None
    feitos = await db.reenvios_feitos(user_id, ids)
    if feitos >= limite:
        return False, 0
    await entregar_packs(bot, user_id, packs, titulo)
    await db.registrar_reenvio(user_id, ids)
    return True, limite - feitos - 1


def texto_limite(limite: int) -> str:
    return (f"🔒 Você já recebeu este conteúdo de novo {limite} vez(es) no chat, que é o limite por compra.\n"
            "Ele continua liberado para ver quando quiser na loja, em <b>Minhas compras</b>.")


def teclado_loja(cfg: Config) -> InlineKeyboardMarkup | None:
    from loja import url_loja
    url = url_loja(cfg)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛍️ Abrir minhas compras",
                                                                       web_app=WebAppInfo(url=url))]])


async def avisar_restantes(bot: Bot, user_id: int, restantes: int | None) -> None:
    if restantes is not None and restantes <= 1:
        aviso = ("Este foi o último reenvio no chat para esta compra." if restantes == 0
                 else "Resta 1 reenvio no chat para esta compra.")
        await bot.send_message(user_id, f"ℹ️ {aviso} Na loja você continua vendo tudo quando quiser.",
                               disable_notification=True)


# ════════════════════════════════════════════════════════════════════════════
#  Bloqueio de fãs
# ════════════════════════════════════════════════════════════════════════════
async def bloquear(bot: Bot, db: Database, cfg: Config, user_id: int) -> str | None:
    """Bloqueia o fã. Devolve uma mensagem de erro ou None se deu certo."""
    if user_id in cfg.admin_ids:
        return "Você não pode bloquear uma conta de criadora."
    await db.banir(user_id, True)
    if await db.assinante_ativo(user_id):
        await db.vencer_agora(user_id)
        await db.encerrar_assinatura(user_id)
        if cfg.vip_channel_id:
            from assinaturas import remover_do_canal
            await remover_do_canal(bot, cfg, user_id)
    log.info("Fã %s bloqueado pela criadora", user_id)
    return None


async def desbloquear(db: Database, user_id: int) -> None:
    await db.banir(user_id, False)


def nome_do_fa(u: dict | None, user_id: int) -> str:
    if not u:
        return f"ID {user_id}"
    nome = html.escape(u.get("nome") or "Sem nome")
    return nome + (f" (@{html.escape(u['username'])})" if u.get("username") else "")


class Bloqueio(BaseMiddleware):
    """Barra mensagens e botões de fãs bloqueados antes de chegar em qualquer handler."""

    def __init__(self, db: Database, cfg: Config):
        self.db, self.cfg = db, cfg

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        if user and user.id not in self.cfg.admin_ids and await self.db.esta_banido(user.id):
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(MSG_BANIDO, show_alert=True)
                elif isinstance(event, Message) and event.chat.type == "private":
                    await event.answer(MSG_BANIDO)
            except Exception:
                pass
            return None
        return await handler(event, data)


def get_router(cfg: Config) -> Router:
    router = Router(name="protecao")
    router.message.filter(F.from_user.id.in_(cfg.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    def _id(command: CommandObject) -> int | None:
        arg = (command.args or "").strip()
        return int(arg) if arg.lstrip("-").isdigit() else None

    @router.message(Command("bloquear"))
    async def cmd_bloquear(msg: Message, command: CommandObject, bot: Bot, db: Database):
        user_id = _id(command)
        if not user_id:
            await msg.answer("Use: <code>/bloquear ID</code>\nO ID aparece nos avisos de venda.")
            return
        erro = await bloquear(bot, db, cfg, user_id)
        await msg.answer(f"⚠️ {erro}" if erro else
                         f"🚫 {nome_do_fa(await db.get_usuario(user_id), user_id)} foi bloqueado(a).\n"
                         f"Para desfazer: <code>/desbloquear {user_id}</code>")

    @router.message(Command("desbloquear"))
    async def cmd_desbloquear(msg: Message, command: CommandObject, db: Database):
        user_id = _id(command)
        if not user_id:
            await msg.answer("Use: <code>/desbloquear ID</code>")
            return
        await desbloquear(db, user_id)
        await msg.answer(f"✅ {nome_do_fa(await db.get_usuario(user_id), user_id)} foi desbloqueado(a).")

    @router.message(Command("bloqueados"))
    async def cmd_bloqueados(msg: Message, db: Database):
        lista = await db.listar_banidos()
        if not lista:
            await msg.answer("Nenhum fã bloqueado.")
            return
        await msg.answer("🚫 <b>Fãs bloqueados</b>\n" + "\n".join(
            f"• {nome_do_fa(u, u['user_id'])} · <code>{u['user_id']}</code>" for u in lista))

    @router.callback_query(F.data.startswith("ban:"))
    async def cb_bloquear(cb: CallbackQuery, bot: Bot, db: Database):
        partes = cb.data.split(":")
        if partes[1] == "nao":
            await cb.answer("Cancelado")
            await cb.message.edit_reply_markup(reply_markup=None)
            return
        if partes[1] == "ok":
            user_id = int(partes[2])
            erro = await bloquear(bot, db, cfg, user_id)
            await cb.answer(erro or "Fã bloqueado", show_alert=bool(erro))
            if not erro:
                await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="↩️ Desbloquear", callback_data=f"desban:{user_id}")]]))
            return
        user_id = int(partes[1])
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Sim, bloquear", callback_data=f"ban:ok:{user_id}"),
            InlineKeyboardButton(text="Cancelar", callback_data="ban:nao"),
        ]]))

    @router.callback_query(F.data.startswith("desban:"))
    async def cb_desbloquear(cb: CallbackQuery, db: Database):
        user_id = int(cb.data.split(":")[1])
        await desbloquear(db, user_id)
        await cb.answer("Desbloqueado")
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Bloquear fã", callback_data=f"ban:{user_id}")]]))

    return router
