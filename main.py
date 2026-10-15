"""
Ponto de entrada: sobe o servidor de webhook (aiohttp) e o bot (long polling) no mesmo processo.

    python main.py
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, MenuButtonWebApp, WebAppInfo
from aiohttp import web

import admin_handlers
import admin_vendas
import assinaturas
import client_handlers
from config import Config, load_config, parse_planos
from database import Database
from payments import criar_gateway
from backup import loop_backup
from remarketing import loop_remarketing
import criadora
import loja
import mensagens
import agenda
import notificacoes
import protecao
from webhook import criar_app_web


def criar_bot(cfg: Config) -> Bot:
    session = None
    if cfg.telegram_api_base:          # simulador local ou Bot API server próprio
        session = AiohttpSession(api=TelegramAPIServer.from_base(cfg.telegram_api_base))
    return Bot(cfg.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def run(cfg: Config) -> None:
    db = Database(cfg.db_path)
    await db.connect()
    await db.criar_planos_padrao(parse_planos(cfg.planos_padrao))   # só na primeira execução
    # nome editado pelo painel do Mini App (Config é imutável, por isso o __setattr__)
    object.__setattr__(cfg, "criadora_nome", await db.get_ajuste("nome", cfg.criadora_nome))
    mensagens.configurar(db, cfg)
    gateway = criar_gateway(cfg)
    bot = criar_bot(cfg)

    # Tudo que for passado aqui é injetado nos handlers pelo nome do parâmetro
    dp = Dispatcher(storage=MemoryStorage(), db=db, cfg=cfg, gateway=gateway)
    dp.message.outer_middleware(protecao.Bloqueio(db, cfg))          # fã bloqueado não passa daqui
    dp.callback_query.outer_middleware(protecao.Bloqueio(db, cfg))
    dp.include_router(admin_handlers.get_router(cfg))   # admin primeiro
    dp.include_router(admin_vendas.get_router(cfg))
    dp.include_router(protecao.get_router(cfg))
    dp.include_router(assinaturas.get_router(cfg))
    dp.include_router(client_handlers.router)
    dp.include_router(client_handlers.fallback_router)   # por último: mensagens livres mostram o menu

    me = await bot.get_me()
    await bot.set_my_commands([BotCommand(command="start", description="Perfil e menu"),
                               BotCommand(command="assinar", description="Assinar o VIP"),
                               BotCommand(command="assinatura", description="Minha assinatura"),
                               BotCommand(command="meus_packs", description="Meus packs comprados"),
                               BotCommand(command="gorjeta", description="Mandar uma gorjeta")])
    for admin_id in cfg.admin_ids:
        try:
            await bot.set_my_commands(
                [BotCommand(command="admin", description="Painel da criadora"),
                 BotCommand(command="novo_pack", description="Criar novo pack"),
                 BotCommand(command="ppv", description="Mensagem paga (pay per view)"),
                 BotCommand(command="novo_combo", description="Criar combo"),
                 BotCommand(command="planos", description="Planos VIP"),
                 BotCommand(command="assinantes", description="Resumo dos assinantes"),
                 BotCommand(command="perfil", description="Foto e bio"),
                 BotCommand(command="cupom", description="Criar cupom"),
                 BotCommand(command="stats", description="Estatísticas de vendas"),
                 BotCommand(command="broadcast", description="Mensagem para compradores"),
                 BotCommand(command="backup", description="Receber backup do banco agora"),
                 BotCommand(command="bloquear", description="Bloquear um fã pelo ID"),
                 BotCommand(command="bloqueados", description="Fãs bloqueados"),
                 BotCommand(command="cancelar", description="Cancelar operação")],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            pass  # o admin ainda não abriu conversa com o bot

    app = criar_app_web(bot, db, gateway, cfg)
    loja.registrar_rotas(app)
    criadora.registrar_rotas(app)
    url = loja.url_loja(cfg)
    if url:   # botão fixo "🛍️ Loja" ao lado do campo de mensagem, no chat com o bot
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🛍️ Loja", web_app=WebAppInfo(url=url)))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port).start()
    logging.info("Bot @%s online | webhook PIX em %s", me.username, cfg.webhook_url.split("?")[0])   # sem o token no log

    jobs = [asyncio.create_task(loop_remarketing(bot, db, cfg)),
            asyncio.create_task(notificacoes.loop_resumo(bot, db, cfg)),
            asyncio.create_task(agenda.loop_agenda(bot, db, cfg))]
    if cfg.backup_ativo:
        jobs.append(asyncio.create_task(loop_backup(bot, db, cfg)))
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        for job in jobs:
            job.cancel()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(load_config()))
