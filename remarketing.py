"""
Remarketing de PIX pendente (job em segundo plano).

A cada REMARKETING_INTERVALO_SEG o job procura vendas que continuam PENDENTES:
  Etapa 1 (LEMBRETE_MINUTOS, padrão 30 min): reenvia o mesmo PIX Copia e Cola.
  Etapa 2 (CUPOM_REMARKETING_MINUTOS, padrão 24 h): cria um cupom PESSOAL
          (só aquele cliente, só aquele item, 1 uso, validade curta) e manda o link com desconto.

Expiração (PIX_VALIDADE_MINUTOS, padrão 60 min): a venda vira EXPIRADA e o cliente recebe
          o botão "🔄 Gerar novo PIX". Se o gateway confirmar o pagamento depois, ainda entrega.

Cada venda recebe no máximo um lembrete e um cupom (coluna vendas.remarketing_etapa).
Quem já liberou o conteúdo por outra compra não recebe nada.
"""
import asyncio
import html
import logging
import secrets
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.deep_linking import create_start_link

from config import Config
from database import Database, Venda, agora, fmt_reais

log = logging.getLogger(__name__)


async def _ja_resolvida(db: Database, v: Venda) -> bool:
    """True se não faz mais sentido insistir nessa venda."""
    packs = await db.packs_do_item(v.pack_id)
    return (not packs
            or await db.tem_todos(v.user_id, [p.id for p in packs])
            or await db.tem_venda_mais_nova(v.user_id, v.pack_id, v.id))


async def _enviar(bot: Bot, db: Database, user_id: int, texto: str, teclado: InlineKeyboardMarkup,
                  silencioso: bool = False) -> bool:
    try:
        await bot.send_message(user_id, texto, reply_markup=teclado, disable_notification=silencioso)
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await bot.send_message(user_id, texto, reply_markup=teclado, disable_notification=silencioso)
        return True
    except TelegramForbiddenError:
        await db.marcar_bloqueado(user_id)
        return False


async def enviar_lembretes(bot: Bot, db: Database, cfg: Config) -> int:
    enviados = 0
    for v in await db.vendas_para_lembrete(agora() - timedelta(minutes=cfg.lembrete_minutos)):
        await db.definir_etapa_remarketing(v.id, 1)       # marca antes: nunca manda duas vezes
        if await _ja_resolvida(db, v):
            continue
        item = await db.get_item(v.pack_id)
        texto = (
            f"⏰ Seu PIX de <b>{html.escape(item.titulo)}</b> ({fmt_reais(v.valor)}) ainda está aguardando pagamento.\n\n"
            "Se ainda quiser o conteúdo, é só pagar com o código abaixo (toque para copiar):\n\n"
            f"<code>{v.pix_copia_cola}</code>"
        )
        teclado = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Já paguei", callback_data=f"pix:{v.pix_id}")
        ]])
        enviados += await _enviar(bot, db, v.user_id, texto, teclado)
    return enviados


async def enviar_cupons(bot: Bot, db: Database, cfg: Config) -> int:
    enviados = 0
    for v in await db.vendas_para_cupom(agora() - timedelta(minutes=cfg.cupom_remarketing_minutos)):
        await db.definir_etapa_remarketing(v.id, 2)
        if await _ja_resolvida(db, v) or v.cupom:        # já veio com cupom: não empilha desconto
            continue
        if not v.pack_id.startswith(("pack_", "combo_")):  # carrinho: o link de cupom só abre pack ou combo
            continue
        item = await db.get_item(v.pack_id)
        codigo = f"VOLTA{secrets.token_hex(2).upper()}"
        await db.criar_cupom(codigo, "pct", cfg.cupom_remarketing_pct, item_id=v.pack_id, user_id=v.user_id,
                             usos_max=1, expira_em=agora() + timedelta(hours=cfg.cupom_remarketing_validade_horas))
        cupom = await db.get_cupom(codigo)
        preco = item.preco - cupom.desconto(item.preco)
        link = await create_start_link(bot, f"{v.pack_id}_{codigo}")
        horas = cfg.cupom_remarketing_validade_horas
        if horas >= 1:
            validade = f"{horas:g} hora{'s' if horas != 1 else ''}"
        else:
            validade = f"{round(horas * 60)} minutos"
        texto = (
            f"🎁 Separei um desconto só pra você: <b>{cfg.cupom_remarketing_pct}% OFF</b> em "
            f"<b>{html.escape(item.titulo)}</b>.\n\n"
            f"De <s>{item.preco_fmt}</s> por <b>{fmt_reais(preco)}</b>, válido por {validade}."
        )
        teclado = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🔓 Liberar por {fmt_reais(preco)}", url=link)
        ]])
        enviados += await _enviar(bot, db, v.user_id, texto, teclado)
    return enviados


def teclado_novo_pix(venda_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Gerar novo PIX", callback_data=f"novopix:{venda_id}")
    ]])


async def expirar_pix(bot: Bot, db: Database) -> int:
    avisados = 0
    for v in await db.vendas_vencidas():
        if not await db.marcar_expirada(v.id) or await _ja_resolvida(db, v):
            continue
        item = await db.get_item(v.pack_id)
        texto = (f"⌛ O PIX de <b>{html.escape(item.titulo)}</b> ({fmt_reais(v.valor)}) expirou.\n"
                 "Se ainda quiser, gere um novo código:")
        avisados += await _enviar(bot, db, v.user_id, texto, teclado_novo_pix(v.id), silencioso=True)
    return avisados


async def rodar_uma_vez(bot: Bot, db: Database, cfg: Config) -> tuple[int, int]:
    if cfg.vip_channel_id:
        from assinaturas import rodar_assinaturas
        await rodar_assinaturas(bot, db, cfg)
    lembretes = await enviar_lembretes(bot, db, cfg)   # antes de expirar: o lembrete vai com o PIX ainda válido
    await expirar_pix(bot, db)
    return lembretes, await enviar_cupons(bot, db, cfg)


async def loop_remarketing(bot: Bot, db: Database, cfg: Config) -> None:
    """Tarefa infinita iniciada pelo main.py."""
    log.info("Remarketing ativo: lembrete em %g min, cupom em %g min",
             cfg.lembrete_minutos, cfg.cupom_remarketing_minutos)
    while True:
        try:
            lembretes, cupons = await rodar_uma_vez(bot, db, cfg)
            if lembretes or cupons:
                log.info("Remarketing: %d lembrete(s), %d cupom(ns)", lembretes, cupons)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erro no job de remarketing")
        await asyncio.sleep(cfg.remarketing_intervalo_seg)
