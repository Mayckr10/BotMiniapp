"""
Avisos para a criadora no chat com o bot.

  - A cada venda paga: o que foi vendido, valor, quem comprou (nome, @, ID), quanto já faturou hoje
    e um botão "🚫 Bloquear fã". Liga/desliga no painel (Ajustes > Notificações).
  - Resumo diário às RESUMO_HORA (padrão 22h, fuso FUSO_HORARIO): faturamento do dia comparado com
    ontem, de onde veio o dinheiro, novos fãs, curtidas, novos VIP, quem vence amanhã e o mais vendido.
"""
import asyncio
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import Config
from database import Database, PedidoPersonalizado, Venda, agora, fmt_reais, iso
from envio import enviar_em_massa

log = logging.getLogger(__name__)

ICONES = {"plano": "⭐", "gorjeta": "💝", "combo": "🎁", "pedido": "✍️", "carrinho": "🛒", "pack": "📦"}


def _icone(item_id: str) -> str:
    return ICONES.get(item_id.split("_", 1)[0], "📦")


def _inicio_do_dia(cfg: Config) -> datetime:
    return datetime.now(ZoneInfo(cfg.fuso_horario)).replace(hour=0, minute=0, second=0, microsecond=0)


async def _enviar(bot: Bot, cfg: Config, texto: str, teclado=None, silencioso: bool = False) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, texto, reply_markup=teclado, disable_notification=silencioso)
        except Exception:
            log.warning("Não consegui avisar a criadora %s", admin_id)


async def avisar_venda(bot: Bot, db: Database, cfg: Config, venda: Venda, detalhe: str = "") -> None:
    """Chamado depois que um pagamento é confirmado e entregue."""
    if await db.get_ajuste("notif_vendas", "1") != "1":
        return
    try:
        item = await db.get_item(venda.pack_id)
        titulo = html.escape(item.titulo if item else venda.pack_id)
        u = await db.get_usuario(venda.user_id) or {}
        quem = html.escape(u.get("nome") or "Fã") + (f" (@{html.escape(u['username'])})" if u.get("username") else "")
        hoje = await db.stats_periodo(_inicio_do_dia(cfg))
        gasto = await db.total_gasto(venda.user_id)
        linhas = [f"💰 <b>Nova venda · {fmt_reais(venda.valor)}</b>",
                  f"{_icone(venda.pack_id)} {titulo}{detalhe}",
                  f"👤 {quem} · <code>{venda.user_id}</code>"]
        if venda.cupom:
            linhas.append(f"🏷️ Cupom {html.escape(venda.cupom)}")
        if gasto["compras"] > 1:
            linhas.append(f"🔁 Já comprou {gasto['compras']} vezes · {fmt_reais(gasto['total'])} no total")
        linhas.append(f"\n📊 Hoje: <b>{fmt_reais(hoje['faturamento'])}</b> em {hoje['vendas']} venda(s)")
        teclado = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚫 Bloquear fã", callback_data=f"ban:{venda.user_id}")]])
        await _enviar(bot, cfg, "\n".join(linhas), teclado)
    except Exception:
        log.exception("Erro ao avisar a venda %s", venda.id)


def _teclado_loja(cfg: Config, texto: str) -> InlineKeyboardMarkup | None:
    from loja import url_loja                 # evita import circular
    url = url_loja(cfg)
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texto, web_app=WebAppInfo(url=url))]])


async def avisar_pedido(bot: Bot, db: Database, cfg: Config, pedido: PedidoPersonalizado) -> None:
    """Pedido personalizado pago: a criadora sempre recebe (é trabalho a fazer, não dá para desligar)."""
    u = await db.get_usuario(pedido.user_id) or {}
    quem = html.escape(u.get("nome") or "Fã") + (f" (@{html.escape(u['username'])})" if u.get("username") else "")
    texto = (f"✍️ <b>Novo pedido personalizado #{pedido.id} · {pedido.preco_fmt}</b>\n"
             f"👤 {quem} · <code>{pedido.user_id}</code>\n\n"
             f"<blockquote>{html.escape(pedido.texto)}</blockquote>\n"
             "Entregue ou recuse pela loja, em <b>Painel › Pedidos</b>.")
    await _enviar(bot, cfg, texto, _teclado_loja(cfg, "✍️ Abrir pedidos"))


async def checar_meta(bot: Bot, db: Database, cfg: Config) -> None:
    """Depois de cada mimo pago: se a meta foi batida agora, avisa a criadora e agradece quem ajudou."""
    try:
        meta = await db.meta_ativa()
        if not meta or meta["batida_em"] or meta["arrecadado"] < meta["alvo"]:
            return
        if not await db.marcar_meta_batida(meta["id"]):
            return
        titulo = html.escape(meta["titulo"])
        await _enviar(bot, cfg, f"🎉 <b>Meta batida: {titulo}</b>\n{fmt_reais(meta['arrecadado'])} de "
                                f"{fmt_reais(meta['alvo'])}, com a ajuda de {meta['fas']} fã(s). "
                                "Quem ajudou recebeu um agradecimento. Encerre ou crie outra em Ajustes › Meta de mimos.")
        fas = [u for u in await db.apoiadores_meta(meta["criado_em"]) if u not in cfg.admin_ids]
        nome = (cfg.criadora_nome or "").split(" ")[0] or "a criadora"

        async def agradecer(user_id: int):
            await bot.send_message(user_id, f"🎉 A meta <b>{titulo}</b> foi batida, e você ajudou! "
                                            f"Muito obrigada pelo carinho 💖\n— {html.escape(nome)}")

        if fas:
            asyncio.create_task(enviar_em_massa(db, cfg, fas, agradecer))
    except Exception:
        log.exception("Erro ao checar a meta de mimos")


def _variacao(hoje: int, ontem: int) -> str:
    if not ontem:
        return ""
    pct = round((hoje - ontem) * 100 / ontem)
    return f" ({'+' if pct >= 0 else ''}{pct}% vs ontem)"


async def montar_resumo(db: Database, cfg: Config) -> str:
    inicio = _inicio_do_dia(cfg)
    offset = int(inicio.utcoffset().total_seconds() // 3600)
    st = await db.stats_periodo(inicio)
    por_dia = await db.vendas_por_dia(inicio - timedelta(days=1), offset)
    ontem = por_dia.get((inicio - timedelta(days=1)).strftime("%Y-%m-%d"), 0)
    tipos = await db.receita_por_tipo(inicio)
    curtidas = await db.curtidas_desde(inicio)
    novos_vip = await db._um("SELECT COUNT(*) AS n FROM vendas WHERE status = 'PAGO' AND pack_id LIKE 'plano_%' "
                             "AND pago_em >= ?", (iso(inicio),))
    amanha = await db._um("SELECT COUNT(*) AS n FROM assinaturas WHERE status = 'ATIVA' AND expira_em > ? "
                          "AND expira_em <= ?", (iso(agora()), iso(agora() + timedelta(days=1))))
    top = await db.top_itens(inicio, 1)
    nome = (cfg.criadora_nome or "").split(" ")[0]

    linhas = [f"🌙 <b>Resumo de hoje{', ' + html.escape(nome) if nome else ''}</b>",
              "",
              f"💰 <b>{fmt_reais(st['faturamento'])}</b> em {st['vendas']} venda(s){_variacao(st['faturamento'], ontem)}"]
    partes = [f"{rot} {fmt_reais(tipos[k])}" for k, rot in (("packs", "📦 Packs"), ("vip", "⭐ VIP"), ("mimos", "💝 Mimos"))
              if tipos.get(k)]
    if partes:
        linhas.append(" · ".join(partes))
    if st["vendas"]:
        linhas.append(f"🎯 Ticket médio {fmt_reais(st['ticket_medio'])}")
    linhas += ["",
               f"👋 {st['novos_usuarios']} fã(s) novo(s) · ❤️ {curtidas} curtida(s)",
               f"⭐ {novos_vip['n']} assinatura(s) VIP hoje"]
    if amanha["n"]:
        linhas.append(f"⏰ {amanha['n']} assinatura(s) vencem nas próximas 24h (o bot já avisou)")
    if top:
        item = await db.get_item(top[0]["pack_id"])
        if item:
            linhas.append(f"🏆 Mais vendido: {html.escape(item.titulo)} ({top[0]['n']}x)")
    if not st["vendas"]:
        linhas += ["", "Dia calmo. Que tal uma publicação nova ou um story amanhã? 💋"]
    return "\n".join(linhas)


async def enviar_resumo(bot: Bot, db: Database, cfg: Config) -> None:
    await _enviar(bot, cfg, await montar_resumo(db, cfg), silencioso=True)


async def loop_resumo(bot: Bot, db: Database, cfg: Config) -> None:
    from backup import segundos_ate
    log.info("Resumo diário às %02d:00 (%s)", cfg.resumo_hora, cfg.fuso_horario)
    while True:
        await asyncio.sleep(segundos_ate(cfg.resumo_hora, cfg.fuso_horario))
        try:
            if await db.get_ajuste("resumo_diario", "1") == "1":
                await enviar_resumo(bot, db, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erro no resumo diário")
        await asyncio.sleep(60)
