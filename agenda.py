"""
Publicações agendadas e lançamentos com contagem regressiva.

A criadora escolhe dia e hora ao publicar (post grátis ou pack pago). Até lá:
  - o post fica escondido;
  - o pack fica fora da vitrine. Se a "contagem regressiva" estiver ligada, os fãs veem um card
    de lançamento com a prévia embaçada, o relógio e o botão "🔔 Me avise".
Na hora marcada este job publica sozinho, posta a prévia no Canal de Amostras (se foi pedido)
e manda mensagem para quem tocou em "Me avise", com o botão de compra.
"""
import asyncio
import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from database import Database
from envio import enviar_em_massa

log = logging.getLogger(__name__)
INTERVALO = 20          # segundos entre verificações


async def publicar_pack(bot: Bot, db: Database, cfg: Config, pack_id: str) -> tuple[bool, bool]:
    """Publica um pack agendado agora. Devolve (publicou?, postou no canal?)."""
    from admin_handlers import postar_no_canal
    publicou, canal = await db.publicar_pack_agendado(pack_id)
    if not publicou:
        return False, False
    pack = await db.get_pack(pack_id)
    no_canal = False
    if canal and cfg.channel_id:
        try:
            await postar_no_canal(bot, cfg, pack)
            no_canal = True
        except Exception:
            log.exception("Agenda: não consegui postar %s no canal", pack_id)
    destinos = await db.quem_pediu_aviso(pack_id)
    if destinos:
        texto = (f"🔥 <b>Acabou de sair: {html.escape(pack.titulo)}</b>\n"
                 f"Você pediu para ser avisado(a). Já dá para desbloquear por {pack.preco_fmt}.")
        teclado = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🔓 Desbloquear · {pack.preco_fmt}", callback_data=f"comprar:{pack.id}")]])
        capa = pack.blur[0]["file_id"] if pack.blur else None

        async def enviar(user_id: int):
            if capa:
                await bot.send_photo(user_id, capa, caption=texto, reply_markup=teclado)
            else:
                await bot.send_message(user_id, texto, reply_markup=teclado)

        asyncio.create_task(enviar_em_massa(db, cfg, destinos, enviar))
    import extras
    await extras.novo_conteudo(bot, db, cfg, pack=pack)
    log.info("Agenda: pack %s publicado (%d aviso(s))", pack_id, len(destinos))
    return True, no_canal


async def rodar_uma_vez(bot: Bot, db: Database, cfg: Config) -> int:
    vencidos = await db.agendados(so_vencidos=True)
    feitos = 0
    import extras
    for p in vencidos["posts"]:
        if await db.publicar_post_agendado(p["id"]):
            feitos += 1
            await extras.novo_conteudo(bot, db, cfg, post=await db.get_post(p["id"]) or {})
    for p in vencidos["packs"]:
        if p["pack"]:
            feitos += (await publicar_pack(bot, db, cfg, p["pack"].id))[0]
    return feitos


async def loop_agenda(bot: Bot, db: Database, cfg: Config) -> None:
    while True:
        try:
            if await rodar_uma_vez(bot, db, cfg):
                log.info("Agenda: publicações agendadas foram ao ar")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erro no job da agenda")
        await asyncio.sleep(INTERVALO)
