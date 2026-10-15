"""
Assinatura VIP (estilo Privacy): o cliente paga um plano por PIX e entra no canal privado.

Como o acesso é protegido:
  - O bot cria UM link do canal VIP com "pedido de entrada" (creates_join_request=True).
  - Quando alguém pede para entrar, o bot só aprova se a pessoa tiver assinatura ativa.
    Se o link vazar, quem não assina é recusado automaticamente.
  - No vencimento o bot remove a pessoa do canal (ban + unban, para ela poder voltar se renovar).

Job (roda junto com o remarketing):
  - ASSINATURA_AVISO_DIAS antes de vencer: aviso com botões de renovação.
  - Venceu: remove do canal e manda os botões para assinar de novo.
Renovar antes do vencimento soma os dias ao período atual.
"""
import html
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.types import ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup

import mensagens
from config import Config
from database import Database, Plano, Venda, agora, fmt_reais

log = logging.getLogger(__name__)


def teclado_renovar(planos: list[Plano], plano_atual: int | None) -> InlineKeyboardMarkup:
    """Renovação em 1 toque: o plano que a pessoa já tem vem primeiro e gera o PIX direto."""
    atual = next((p for p in planos if p.id == plano_atual), None) or next((p for p in planos if p.dias <= 31), None)
    teclado = teclado_planos([p for p in planos if not atual or p.id != atual.id])
    if atual:
        teclado.inline_keyboard.insert(0, [InlineKeyboardButton(text=f"🔄 Renovar {atual.nome} · {atual.preco_fmt}",
                                                                callback_data=f"comprar:{atual.item_id}")])
    return teclado


def teclado_planos(planos: list[Plano], titulo_extra: str = "") -> InlineKeyboardMarkup:
    linhas = []
    mensal = next((p for p in planos if p.dias == 30), None)
    for p in planos:
        rotulo = f"⭐ {p.nome} · {p.preco_fmt}"
        if mensal and p.dias > 30:                         # mostra a economia em relação ao mensal
            cheio = mensal.preco * p.dias // 30
            if cheio > p.preco:
                rotulo += f" (-{round(100 - p.preco * 100 / cheio)}%)"
        linhas.append([InlineKeyboardButton(text=rotulo, callback_data=f"comprar:{p.item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=linhas)


def data_local(iso_ou_dt, cfg: Config) -> str:
    dt = datetime.fromisoformat(iso_ou_dt) if isinstance(iso_ou_dt, str) else iso_ou_dt
    return dt.astimezone(ZoneInfo(cfg.fuso_horario)).strftime("%d/%m/%Y às %H:%M")


async def link_vip(bot: Bot, db: Database, cfg: Config) -> str:
    """Link fixo do canal VIP com pedido de entrada (criado uma vez e guardado no banco)."""
    link = await db.get_ajuste("link_vip")
    if not link:
        convite = await bot.create_chat_invite_link(cfg.vip_channel_id, name="Assinantes (bot)",
                                                    creates_join_request=True)
        link = convite.invite_link
        await db.set_ajuste("link_vip", link)
    return link


async def esta_no_canal(bot: Bot, cfg: Config, user_id: int) -> bool:
    try:
        membro = await bot.get_chat_member(cfg.vip_channel_id, user_id)
        return membro.status in ("member", "administrator", "creator")
    except Exception:
        return False


async def ativar_assinatura(bot: Bot, db: Database, cfg: Config, venda: Venda, plano: Plano) -> None:
    """Chamado pelo webhook quando o PIX de um plano é pago."""
    vence = await db.estender_assinatura(venda.user_id, plano.id, plano.dias)
    try:
        await bot.unban_chat_member(cfg.vip_channel_id, venda.user_id, only_if_banned=True)
    except Exception:
        pass
    if await esta_no_canal(bot, cfg, venda.user_id):
        await bot.send_message(venda.user_id, f"⭐ <b>Assinatura renovada!</b>\nVálida até {data_local(vence, cfg)}.")
    else:
        try:
            teclado = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔐 Entrar no canal VIP", url=await link_vip(bot, db, cfg))
            ]])
            await bot.send_message(
                venda.user_id,
                f"{await mensagens.para_fa('vip', venda.user_id, plano.nome)}\n"
                f"Sua assinatura vale até {data_local(vence, cfg)}.\n\n"
                "Toque no botão para pedir a entrada no canal. A aprovação é automática.",
                reply_markup=teclado,
            )
        except Exception as e:
            # Pagamento e assinatura já estão salvos; só o link do canal falhou (bot sem permissão, ID errado...)
            log.exception("Não consegui gerar o link do canal VIP")
            await bot.send_message(venda.user_id, f"⭐ <b>Assinatura ativada até {data_local(vence, cfg)}!</b>\n"
                                                  "Estou preparando seu acesso ao canal e já te aviso.")
            for admin_id in cfg.admin_ids:
                try:
                    await bot.send_message(admin_id, "⚠️ Não consegui gerar o link do canal VIP "
                                                     f"(<code>{html.escape(str(e))}</code>). Confira se o bot é admin do canal "
                                                     "com permissão de convidar usuários e se VIP_CHANNEL_ID está certo.")
                except Exception:
                    pass


async def remover_do_canal(bot: Bot, cfg: Config, user_id: int) -> None:
    try:
        await bot.ban_chat_member(cfg.vip_channel_id, user_id)
        await bot.unban_chat_member(cfg.vip_channel_id, user_id, only_if_banned=True)   # pode voltar se renovar
    except Exception:
        log.exception("Não consegui remover %s do canal VIP", user_id)


async def rodar_assinaturas(bot: Bot, db: Database, cfg: Config) -> tuple[int, int]:
    """Avisos de vencimento e remoção de quem venceu."""
    planos = await db.listar_planos()
    avisos = removidos = 0
    # 1º aviso ASSINATURA_AVISO_DIAS antes (padrão 3), 2º aviso 1 dia antes; os dois com renovação em 1 toque
    etapas = [(2, 1.0, "⏰ <b>Sua assinatura VIP vence amanhã</b> ({data}).\nToque abaixo e renove em 1 toque, "
                       "os dias são somados:"),
              (1, cfg.assinatura_aviso_dias, "⏰ Sua assinatura VIP vence em {data}.\n"
                                             "Renove agora para não perder o acesso (os dias são somados):")]
    for etapa, dias, modelo in etapas:
        for a in await db.assinaturas_a_avisar(agora() + timedelta(days=dias), etapa):
            await db.marcar_aviso_assinatura(a.user_id, etapa)
            try:
                await bot.send_message(a.user_id, modelo.format(data=data_local(a.expira_em, cfg)),
                                       reply_markup=teclado_renovar(planos, a.plano_id))
                avisos += 1
            except Exception:
                pass
    for a in await db.assinaturas_vencidas():
        await db.encerrar_assinatura(a.user_id)
        await remover_do_canal(bot, cfg, a.user_id)
        removidos += 1
        try:
            await bot.send_message(a.user_id, "💔 Sua assinatura VIP venceu e o acesso ao canal foi encerrado.\n"
                                              "Quer voltar? É só escolher um plano:",
                                   reply_markup=teclado_renovar(planos, a.plano_id))
        except Exception:
            pass
    return avisos, removidos


def get_router(cfg: Config) -> Router:
    router = Router(name="assinaturas")

    @router.chat_join_request(F.chat.id == cfg.vip_channel_id)
    async def pedido_de_entrada(req: ChatJoinRequest, bot: Bot, db: Database):
        if req.from_user.id in cfg.admin_ids or (await db.assinante_ativo(req.from_user.id)
                                                 and not await db.esta_banido(req.from_user.id)):
            await req.approve()
            log.info("VIP: entrada aprovada para %s", req.from_user.id)
            return
        await req.decline()
        try:
            await bot.send_message(req.user_chat_id, "🔒 O canal VIP é só para assinantes. Escolha um plano para entrar:",
                                   reply_markup=teclado_planos(await db.listar_planos()))
        except Exception:
            pass

    return router
