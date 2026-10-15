"""
/resetar_banco: apaga TODO o banco de dados (packs, vendas, fãs, assinaturas, posts, ajustes...).

Só existe para os IDs de RESET_IDS (padrão 8086722916). Para qualquer outra pessoa o comando
não aparece no menu e o bot age como se ele não existisse.

Segurança:
  - pede confirmação em duas etapas (botão e depois "Sim, apagar tudo");
  - antes de apagar, manda o backup do banco no chat de quem pediu, para dar para voltar atrás;
  - os botões só funcionam por 5 minutos.

Não mexe no Telegram: quem está no canal VIP continua lá, e as mensagens já enviadas continuam.
"""
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Config
from database import Database

log = logging.getLogger(__name__)
VALIDADE = 300          # segundos para confirmar


def _teclado(etapa: str) -> InlineKeyboardMarkup:
    agora = int(time.time())
    if etapa == "1":
        sim = InlineKeyboardButton(text="⚠️ Quero apagar tudo", callback_data=f"reset:1:{agora}")
    else:
        sim = InlineKeyboardButton(text="🗑️ Sim, apagar tudo", callback_data=f"reset:2:{agora}")
    return InlineKeyboardMarkup(inline_keyboard=[[sim], [InlineKeyboardButton(text="Cancelar", callback_data="reset:nao")]])


def get_router(cfg: Config) -> Router:
    router = Router(name="reset_banco")
    router.message.filter(F.from_user.id.in_(cfg.reset_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.reset_ids))

    @router.message(Command("resetar_banco"))
    async def cmd_reset(msg: Message, db: Database):
        st = await db._um("SELECT (SELECT COUNT(*) FROM usuarios) AS fas, (SELECT COUNT(*) FROM packs) AS packs, "
                          "(SELECT COUNT(*) FROM vendas WHERE status = 'PAGO') AS vendas, "
                          "(SELECT COUNT(*) FROM assinaturas WHERE status = 'ATIVA') AS vip")
        await msg.answer(
            "🧨 <b>Resetar o banco de dados</b>\n\n"
            "Isso apaga <b>tudo</b>: packs, combos, posts, vendas, fãs, assinaturas, cupons, planos, "
            "bloqueios e ajustes do painel (inclusive perfil e conta de pagamento).\n\n"
            f"Hoje: {st['fas']} fã(s) · {st['packs']} pack(s) · {st['vendas']} venda(s) paga(s) · "
            f"{st['vip']} VIP ativo(s).\n\n"
            "Antes de apagar eu mando o backup aqui no chat. Quem está no canal VIP continua lá.",
            reply_markup=_teclado("1"))

    @router.callback_query(F.data.startswith("reset:"))
    async def cb_reset(cb: CallbackQuery, bot: Bot, db: Database):
        partes = cb.data.split(":")
        if partes[1] == "nao":
            await cb.answer("Cancelado")
            await cb.message.edit_text("Reset cancelado. Nada foi apagado.")
            return
        if time.time() - int(partes[2]) > VALIDADE:
            await cb.answer("Expirou. Mande /resetar_banco de novo.", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=None)
            return
        if partes[1] == "1":
            await cb.answer()
            await cb.message.edit_text("⚠️ <b>Última confirmação.</b>\nDepois disso não dá para desfazer pelo bot "
                                       "(só restaurando o backup).", reply_markup=_teclado("2"))
            return

        await cb.answer("Apagando…")
        await cb.message.edit_reply_markup(reply_markup=None)
        try:
            from backup import gerar_backup
            dados, nome = await gerar_backup(db, cfg.fuso_horario)
            await bot.send_document(cb.from_user.id, BufferedInputFile(dados, filename=f"antes-do-reset-{nome}"),
                                    caption="💾 Backup de antes do reset.")
        except Exception:
            log.exception("Reset: não consegui mandar o backup")
            await cb.message.answer("❌ Não consegui gerar ou enviar o backup, então <b>não apaguei nada</b>.")
            return
        tabelas = await db.apagar_tudo()
        log.warning("Banco resetado por %s (%d tabelas)", cb.from_user.id, len(tabelas))
        await cb.message.answer(f"✅ Banco zerado ({len(tabelas)} tabelas).\n"
                                f"Tabelas: <code>{html.escape(', '.join(tabelas))}</code>\n\n"
                                "Recomendo reiniciar o serviço no Railway para limpar o que ficou em memória.")

    return router
