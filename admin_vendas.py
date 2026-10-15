"""
Ferramentas de vendas da criadora (chat privado, só admins):

  /cupom CODIGO 10% [pack_xxxx] [usos=50] [dias=7]   cria cupom percentual
  /cupom CODIGO 5,00 [combo_xxxx] ...                 cria cupom de valor fixo
  /cupons                                             lista cupons ativos
  /cupom_off CODIGO                                   desativa um cupom
  /novo_combo                                         monta um combo de packs e publica no canal
  /combos                                             lista combos e links
  /stats                                              vendas do dia/semana/mês, ticket médio, conversão
  /broadcast [pack_xxxx]                              mensagem para todos os compradores (ou de um pack)
  /reenviar USER_ID pack_xxxx                         libera e reenvia um pack manualmente
  /backup                                             recebe agora o backup do banco
  /planos, /plano Nome dias preço, /plano_off ID      planos da assinatura VIP
  /assinantes                                         resumo dos assinantes
  /dar_vip USER_ID dias                               VIP de cortesia (ou para corrigir um caso)
  /remover_vip USER_ID                                encerra a assinatura agora (reembolso, estorno)
  /perfil                                             foto e bio mostradas no /start
  (responder a uma mensagem de fã)                    a resposta vai para o fã
"""
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)
from aiogram.utils.deep_linking import create_start_link

from admin_handlers import _resumo_midias, parse_preco
from backup import enviar_backup
from envio import enviar_em_massa, resumo_envio
from config import Config
from assinaturas import data_local
from database import Combo, Database, agora, fmt_reais, iso
from webhook import entregar_pack

log = logging.getLogger(__name__)

RE_CODIGO = re.compile(r"^[A-Za-z0-9]{2,30}$")
RE_ITEM = re.compile(r"^(?:pack|combo)_[0-9a-f]{4,}$")


class NovoCombo(StatesGroup):
    packs = State()
    titulo = State()
    preco = State()
    confirmar = State()


class Perfil(StatesGroup):
    foto = State()
    bio = State()


class Broadcast(StatesGroup):
    mensagem = State()
    confirmar = State()


# ════════════════════════════════════════════════════════════════════════════
#  Broadcast (função pura, testável)
# ════════════════════════════════════════════════════════════════════════════
async def enviar_broadcast(bot: Bot, db: Database, cfg: Config, from_chat_id: int, message_id: int,
                           destinos: list[int]) -> dict:
    """Copia uma mensagem da admin para cada destino (ver envio.py para limites e erros)."""
    return await enviar_em_massa(db, cfg, destinos, lambda uid: bot.copy_message(uid, from_chat_id, message_id))


# ════════════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════════════
def _parse_cupom(args: str) -> tuple[dict | None, str]:
    """'VERAO10 10% pack_8f3a usos=50 dias=7' -> dict com os campos do cupom."""
    partes = args.split()
    if len(partes) < 2:
        return None, "uso"
    codigo, valor, extras = partes[0], partes[1], partes[2:]
    if not RE_CODIGO.match(codigo):
        return None, "O código deve ter de 2 a 30 letras/números, sem espaços ou símbolos."
    d: dict = {"codigo": codigo.upper(), "item_id": None, "usos_max": None, "expira_em": None}
    if valor.endswith("%"):
        try:
            pct = int(valor[:-1])
        except ValueError:
            return None, "Percentual inválido."
        if not 1 <= pct <= 90:
            return None, "O percentual deve ficar entre 1% e 90%."
        d["tipo"], d["valor"] = "pct", pct
    else:
        centavos = parse_preco(valor)
        if not centavos:
            return None, "Valor inválido. Use 10% ou 5,00."
        d["tipo"], d["valor"] = "fixo", centavos
    for e in extras:
        if RE_ITEM.match(e):
            d["item_id"] = e
        elif e.startswith("usos=") and e[5:].isdigit():
            d["usos_max"] = int(e[5:])
        elif e.startswith("dias=") and e[5:].replace(".", "", 1).isdigit():
            d["expira_em"] = agora() + timedelta(days=float(e[5:]))
        else:
            return None, f"Não entendi <code>{html.escape(e)}</code>."
    return d, ""


AJUDA_CUPOM = (
    "🏷️ <b>Criar cupom</b>\n"
    "<code>/cupom CODIGO 10%</code>  (percentual)\n"
    "<code>/cupom CODIGO 5,00</code>  (valor fixo)\n\n"
    "Opcionais: <code>pack_xxxx</code> ou <code>combo_xxxx</code> (só vale para ele), "
    "<code>usos=50</code>, <code>dias=7</code>\n"
    "Ex: <code>/cupom VERAO10 10% usos=100 dias=3</code>\n\n"
    "O cliente usa pelo link: <code>t.me/SeuBot?start=pack_xxxx_VERAO10</code>"
)


def legenda_combo(combo: Combo, packs: list) -> str:
    soma = sum(p.preco for p in packs)
    midias = [m for p in packs for m in p.originais]
    economia = f"💸 Separados: <s>{fmt_reais(soma)}</s>\n" if soma > combo.preco else ""
    return (
        f"🎁 <b>{html.escape(combo.titulo)}</b>\n\n"
        f"{len(packs)} packs · {_resumo_midias(midias)}\n"
        + "".join(f"• {html.escape(p.titulo)}\n" for p in packs)
        + f"\n{economia}💰 <b>{combo.preco_fmt}</b>\n\n👇 Toque no botão para liberar tudo."
    )


async def postar_combo_no_canal(bot: Bot, cfg: Config, combo: Combo, packs: list) -> None:
    link = await create_start_link(bot, combo.id)
    teclado = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔓 Liberar Combo - {combo.preco_fmt}", url=link)
    ]])
    capas = [p.blur[0]["file_id"] for p in packs if p.blur][:10]   # uma amostra de cada pack
    if len(capas) == 1:
        await bot.send_photo(cfg.channel_id, capas[0], caption=legenda_combo(combo, packs), reply_markup=teclado)
        return
    if capas:
        await bot.send_media_group(cfg.channel_id, [InputMediaPhoto(media=c) for c in capas])
    await bot.send_message(cfg.channel_id, legenda_combo(combo, packs), reply_markup=teclado)


def _teclado_packs(packs, escolhidos: list[str]) -> InlineKeyboardMarkup:
    linhas = [[InlineKeyboardButton(
        text=f"{'✅' if p.id in escolhidos else '⬜'} {p.titulo} · {p.preco_fmt}",
        callback_data=f"cb:tg:{p.id}")] for p in packs]
    linhas.append([InlineKeyboardButton(text=f"➡️ Continuar ({len(escolhidos)})", callback_data="cb:ok"),
                   InlineKeyboardButton(text="✖️ Cancelar", callback_data="cb:cancelar")])
    return InlineKeyboardMarkup(inline_keyboard=linhas)


def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%".replace(".", ",") if x is not None else "—"


# ════════════════════════════════════════════════════════════════════════════
#  Router
# ════════════════════════════════════════════════════════════════════════════
def get_router(cfg: Config) -> Router:
    router = Router(name="admin_vendas")
    router.message.filter(F.chat.type == "private", F.from_user.id.in_(cfg.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    # ── Cupons ─────────────────────────────────────────────────────────────
    @router.message(Command("cupom"))
    async def cupom(msg: Message, command: CommandObject, db: Database, bot: Bot):
        d, erro = _parse_cupom(command.args or "")
        if not d:
            await msg.answer(AJUDA_CUPOM if erro == "uso" else f"⚠️ {erro}\n\n{AJUDA_CUPOM}")
            return
        if d["item_id"] and not await db.get_item(d["item_id"]):
            await msg.answer(f"⚠️ Não encontrei <code>{d['item_id']}</code>. Veja os ids em /packs ou /combos.")
            return
        await db.criar_cupom(**d)
        c = await db.get_cupom(d["codigo"])
        regras = [c.descricao]
        regras.append(f"só para <code>{c.item_id}</code>" if c.item_id else "vale para qualquer pack/combo")
        if c.usos_max:
            regras.append(f"{c.usos_max} usos")
        if d["expira_em"]:
            regras.append(f"até {d['expira_em'].astimezone(ZoneInfo(cfg.fuso_horario)):%d/%m %H:%M}")
        exemplo = c.item_id or ((await db.listar_packs()) or [None])[0]
        exemplo = exemplo.id if hasattr(exemplo, "id") else exemplo
        link = await create_start_link(bot, f"{exemplo}_{c.codigo}") if exemplo else None
        await msg.answer(
            f"✅ Cupom <b>{c.codigo}</b> criado: " + " · ".join(regras)
            + (f"\n\n🔗 Link com o cupom:\n{link}" if link else "")
            + ("" if c.item_id else "\n\nPara outro pack, adicione <code>_" + c.codigo + "</code> ao final do link dele.")
        )

    @router.message(Command("cupons"))
    async def cupons(msg: Message, db: Database):
        lista = await db.listar_cupons()
        if not lista:
            await msg.answer("Nenhum cupom ativo.\n\n" + AJUDA_CUPOM)
            return
        linhas = []
        for c in lista:
            usos = f"{c.usos}/{c.usos_max}" if c.usos_max else f"{c.usos} usos"
            alvo = c.item_id or "todos"
            expirado = " · <i>expirado</i>" if c.expira_em and c.expira_em < iso(agora()) else ""
            linhas.append(f"• <b>{c.codigo}</b> {c.descricao} · {alvo} · {usos}{expirado}")
        await msg.answer("🏷️ <b>Cupons ativos</b>\n\n" + "\n".join(linhas) + "\n\nDesativar: /cupom_off CODIGO")

    @router.message(Command("cupom_off"))
    async def cupom_off(msg: Message, command: CommandObject, db: Database):
        codigo = (command.args or "").strip()
        ok = codigo and await db.desativar_cupom(codigo)
        await msg.answer(f"🗑️ Cupom <b>{html.escape(codigo.upper())}</b> desativado." if ok
                         else "Use: <code>/cupom_off CODIGO</code> (veja em /cupons)")

    # ── Combos ─────────────────────────────────────────────────────────────
    @router.message(Command("novo_combo"))
    async def novo_combo(msg: Message, state: FSMContext, db: Database):
        packs = await db.listar_packs()
        if len(packs) < 2:
            await msg.answer("Você precisa de pelo menos 2 packs para montar um combo. Use /novo_pack.")
            return
        await state.clear()
        await state.set_state(NovoCombo.packs)
        await state.update_data(escolhidos=[])
        await msg.answer("🎁 <b>Novo combo</b>\nMarque os packs que entram no combo:",
                         reply_markup=_teclado_packs(packs, []))

    @router.callback_query(NovoCombo.packs, F.data.startswith("cb:tg:"))
    async def alternar_pack(cb: CallbackQuery, state: FSMContext, db: Database):
        pack_id = cb.data[len("cb:tg:"):]
        escolhidos = (await state.get_data())["escolhidos"]
        escolhidos = [p for p in escolhidos if p != pack_id] if pack_id in escolhidos else escolhidos + [pack_id]
        await state.update_data(escolhidos=escolhidos)
        await cb.message.edit_reply_markup(reply_markup=_teclado_packs(await db.listar_packs(), escolhidos))
        await cb.answer()

    @router.callback_query(NovoCombo.packs, F.data == "cb:ok")
    async def packs_ok(cb: CallbackQuery, state: FSMContext, db: Database):
        escolhidos = (await state.get_data())["escolhidos"]
        if len(escolhidos) < 2:
            await cb.answer("Escolha pelo menos 2 packs.", show_alert=True)
            return
        packs = [await db.get_pack(p) for p in escolhidos]
        soma = sum(p.preco for p in packs)
        await state.update_data(soma=soma)
        await state.set_state(NovoCombo.titulo)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(f"{len(packs)} packs escolhidos (somam {fmt_reais(soma)}).\n\n"
                                "✏️ Envie o <b>título</b> do combo:")
        await cb.answer()

    @router.callback_query(F.data == "cb:cancelar")
    async def combo_cancelar(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("❌ Combo cancelado.")
        await cb.answer()

    @router.message(NovoCombo.titulo, F.text)
    async def combo_titulo(msg: Message, state: FSMContext):
        await state.update_data(titulo=msg.text.strip()[:100])
        soma = (await state.get_data())["soma"]
        sugestao = fmt_reais((soma * 75 // 100) // 100 * 100 - 10 if soma >= 300 else soma)   # ~25% OFF, final ,90
        await state.set_state(NovoCombo.preco)
        await msg.answer(f"💰 Qual o <b>preço</b> do combo? Separados custam {fmt_reais(soma)}.\n"
                         f"Ex: <code>{sugestao.replace('R$ ', '')}</code>")

    @router.message(NovoCombo.preco, F.text)
    async def combo_preco(msg: Message, state: FSMContext, db: Database):
        preco = parse_preco(msg.text)
        if preco is None:
            await msg.answer("⚠️ Preço inválido. Envie algo como <code>49,90</code>.")
            return
        await state.update_data(preco=preco)
        await state.set_state(NovoCombo.confirmar)
        d = await state.get_data()
        packs = [await db.get_pack(p) for p in d["escolhidos"]]
        previa = Combo(id="—", titulo=d["titulo"], preco=preco, pack_ids=d["escolhidos"])
        teclado = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Publicar no canal", callback_data="cb:publicar")],
            [InlineKeyboardButton(text="💾 Só salvar (sem postar)", callback_data="cb:salvar")],
            [InlineKeyboardButton(text="✖️ Cancelar", callback_data="cb:cancelar")],
        ])
        await msg.answer("📝 <b>Confira o combo:</b>\n\n" + legenda_combo(previa, packs), reply_markup=teclado)

    @router.callback_query(NovoCombo.confirmar, F.data.in_({"cb:publicar", "cb:salvar"}))
    async def combo_publicar(cb: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
        d = await state.get_data()
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer()
        combo_id = await db.criar_combo(d["titulo"], d["preco"], d["escolhidos"])
        combo = await db.get_combo(combo_id)
        link = await create_start_link(bot, combo_id)
        if cb.data == "cb:salvar":
            await cb.message.answer(f"💾 Combo <code>{combo_id}</code> salvo.\n🔗 {link}")
            return
        try:
            await postar_combo_no_canal(bot, cfg, combo, await db.packs_do_item(combo_id))
            await cb.message.answer(f"✅ Combo <code>{combo_id}</code> publicado no canal!\n🔗 {link}")
        except Exception as e:
            log.exception("Falha ao postar combo")
            await cb.message.answer(f"⚠️ Combo salvo, mas não consegui postar: <code>{html.escape(str(e))}</code>\n🔗 {link}")

    @router.message(Command("combos"))
    async def combos(msg: Message, db: Database, bot: Bot):
        lista = await db.listar_combos()
        if not lista:
            await msg.answer("Nenhum combo ainda. Use /novo_combo.")
            return
        linhas = [f"• <b>{html.escape(c.titulo)}</b> – {c.preco_fmt} ({len(c.pack_ids)} packs)\n"
                  f"  <code>{await create_start_link(bot, c.id)}</code>" for c in lista]
        await msg.answer("🎁 <b>Combos</b>\n\n" + "\n".join(linhas))

    # ── Stats ──────────────────────────────────────────────────────────────
    @router.message(Command("stats"))
    async def stats(msg: Message, db: Database):
        fuso = ZoneInfo(cfg.fuso_horario)
        hoje = datetime.now(fuso).replace(hour=0, minute=0, second=0, microsecond=0)
        periodos = [("Hoje", hoje), ("7 dias", agora() - timedelta(days=7)),
                    ("30 dias", agora() - timedelta(days=30))]
        blocos = []
        for nome, desde in periodos:
            s = await db.stats_periodo(desde)
            blocos.append(
                f"<b>{nome}</b>\n"
                f"💰 {fmt_reais(s['faturamento'])} em {s['vendas']} venda(s) · ticket médio {fmt_reais(s['ticket_medio'])}\n"
                f"🔗 {s['cliques']} abertura(s) de link → {s['compras_unicas']} compra(s) · conversão {_pct(s['conversao'])}\n"
                f"🏷️ {s['com_cupom']} com cupom · 👤 {s['novos_usuarios']} novo(s) cliente(s)"
            )
        mes = await db.stats_periodo(agora() - timedelta(days=30))
        top = ""
        if mes["top_item"]:
            item = await db.get_item(mes["top_item"])
            nome_top = html.escape(item.titulo) if item else mes["top_item"]
            top = f"\n\n🏆 Mais vendido (30 dias): <b>{nome_top}</b> ({mes['top_qtd']}x)"
        pend = await db.pendentes_abertos()
        vip = await db.resumo_assinaturas()
        top += (f"\n⭐ Assinantes ativos: {vip['ativos']} ({vip['vencendo_7d']} vencem em 7 dias) · "
                f"receita VIP 30 dias: {fmt_reais(vip['receita_30d'])}")
        await msg.answer("📊 <b>Estatísticas</b>\n\n" + "\n\n".join(blocos) + top
                         + f"\n⏳ PIX pendentes nas últimas 24h: {pend}"
                         + "\n\n<i>Conversão = compras ÷ pessoas que abriram o link de cada item.</i>")

    # ── Broadcast ──────────────────────────────────────────────────────────
    @router.message(Command("broadcast"))
    async def broadcast(msg: Message, command: CommandObject, state: FSMContext, db: Database):
        alvo = (command.args or "").strip() or None
        if alvo and not RE_ITEM.match(alvo):
            await msg.answer("Use <code>/broadcast</code> (todos os compradores) ou <code>/broadcast pack_xxxx</code>.")
            return
        destinos = await db.compradores(alvo)
        if not destinos:
            await msg.answer("Ainda não há compradores para receber a mensagem.")
            return
        await state.set_state(Broadcast.mensagem)
        await state.update_data(alvo=alvo)
        quem = f"compradores de <code>{alvo}</code>" if alvo else "todos os compradores"
        await msg.answer(f"📣 <b>Broadcast</b> para {quem} ({len(destinos)}).\n\n"
                         "Envie a mensagem exatamente como deve chegar (texto, foto ou vídeo com legenda).\n"
                         "/cancelar para desistir.")

    @router.message(Broadcast.mensagem, ~F.text.startswith("/"))
    async def broadcast_msg(msg: Message, state: FSMContext, db: Database):
        alvo = (await state.get_data())["alvo"]
        n = len(await db.compradores(alvo))
        await state.update_data(msg_id=msg.message_id)
        await state.set_state(Broadcast.confirmar)
        teclado = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"📤 Enviar para {n}", callback_data="bc:enviar"),
            InlineKeyboardButton(text="✖️ Cancelar", callback_data="bc:cancelar"),
        ]])
        await msg.reply("👆 Essa é a mensagem. Confirmar envio?", reply_markup=teclado)

    @router.callback_query(Broadcast.confirmar, F.data == "bc:enviar")
    async def broadcast_enviar(cb: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
        d = await state.get_data()
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        destinos = await db.compradores(d["alvo"])
        await cb.answer("Enviando…")
        await cb.message.answer(f"⏳ Enviando para {len(destinos)} pessoa(s)…")

        async def tarefa():
            res = await enviar_broadcast(bot, db, cfg, cb.message.chat.id, d["msg_id"], destinos)
            await bot.send_message(cb.message.chat.id,
                                   "✅ Broadcast concluído.\n" + resumo_envio(res))
        asyncio.create_task(tarefa())        # não trava o bot enquanto envia

    @router.callback_query(F.data == "bc:cancelar")
    async def broadcast_cancelar(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("❌ Broadcast cancelado.")
        await cb.answer()

    # ── Planos e assinantes ────────────────────────────────────────────────
    @router.message(Command("planos"))
    async def planos(msg: Message, db: Database):
        lista = await db.listar_planos()
        linhas = [f"• <b>{html.escape(p.nome)}</b> (id {p.id}) · {p.dias} dias · {p.preco_fmt}" for p in lista]
        await msg.answer("⭐ <b>Planos VIP</b>\n\n" + ("\n".join(linhas) or "Nenhum plano ativo.")
                         + "\n\nCriar/alterar: <code>/plano Mensal 30 29,90</code>\nDesativar: <code>/plano_off ID</code>"
                         "\nLink direto para assinar: <code>t.me/SeuBot?start=assinar</code>")

    @router.message(Command("plano"))
    async def plano(msg: Message, command: CommandObject, db: Database):
        partes = (command.args or "").split()
        preco = parse_preco(partes[-1]) if len(partes) >= 3 else None
        if len(partes) < 3 or not partes[-2].isdigit() or not preco:
            await msg.answer("Use: <code>/plano Nome dias preço</code>\nEx: <code>/plano Trimestral 90 79,90</code>")
            return
        nome, dias = " ".join(partes[:-2]), int(partes[-2])
        plano_id = await db.salvar_plano(nome, dias, preco)
        await msg.answer(f"✅ Plano <b>{html.escape(nome)}</b> (id {plano_id}): {dias} dias por {fmt_reais(preco)}.")

    @router.message(Command("plano_off"))
    async def plano_off(msg: Message, command: CommandObject, db: Database):
        arg = (command.args or "").strip()
        ok = arg.isdigit() and await db.desativar_plano(int(arg))
        await msg.answer("🗑️ Plano desativado. Quem já assinou mantém o acesso até vencer." if ok
                         else "Use: <code>/plano_off ID</code> (veja os ids em /planos)")

    @router.message(Command("assinantes"))
    async def assinantes(msg: Message, db: Database):
        r = await db.resumo_assinaturas()
        await msg.answer(f"⭐ <b>Assinantes</b>\n\nAtivos: <b>{r['ativos']}</b>\nVencem nos próximos 7 dias: {r['vencendo_7d']}\n"
                         f"Receita de assinaturas (30 dias): {fmt_reais(r['receita_30d'])}\n\n"
                         "Para falar com todos: <code>/broadcast</code> envia para compradores; "
                         "<code>/ppv</code> envia mensagem paga para assinantes.")

    @router.message(Command("dar_vip"))
    async def dar_vip(msg: Message, command: CommandObject, db: Database, bot: Bot):
        from assinaturas import link_vip
        partes = (command.args or "").split()
        if len(partes) != 2 or not partes[0].isdigit() or not partes[1].isdigit():
            await msg.answer("Use: <code>/dar_vip USER_ID dias</code>")
            return
        user_id, dias = int(partes[0]), int(partes[1])
        vence = await db.estender_assinatura(user_id, None, dias)
        try:
            await bot.unban_chat_member(cfg.vip_channel_id, user_id, only_if_banned=True)
            teclado = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔐 Entrar no canal VIP", url=await link_vip(bot, db, cfg))]])
            await bot.send_message(user_id, f"🎁 Você ganhou acesso VIP até {data_local(vence, cfg)}!", reply_markup=teclado)
            await msg.answer(f"✅ VIP liberado para <code>{user_id}</code> até {data_local(vence, cfg)}.")
        except Exception as e:
            await msg.answer(f"✅ VIP salvo até {data_local(vence, cfg)}, mas não consegui avisar a pessoa: "
                             f"<code>{html.escape(str(e))}</code>")

    @router.message(Command("remover_vip"))
    async def remover_vip(msg: Message, command: CommandObject, db: Database):
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await msg.answer("Use: <code>/remover_vip USER_ID</code> (ex: reembolso ou estorno)")
            return
        ok = await db.vencer_agora(int(arg))
        await msg.answer("✅ Assinatura encerrada. A pessoa sai do canal VIP em instantes." if ok
                         else "Essa pessoa não tem assinatura ativa.")

    # ── Perfil ─────────────────────────────────────────────────────────────
    @router.message(Command("perfil"))
    async def perfil(msg: Message, state: FSMContext):
        await state.set_state(Perfil.foto)
        await msg.answer("🖼️ Envie a <b>foto de perfil</b> que aparece no /start (ou /pular para manter a atual).")

    @router.message(Perfil.foto, F.photo | (F.text == "/pular"))
    async def perfil_foto(msg: Message, state: FSMContext, db: Database):
        if msg.photo:
            await db.set_ajuste("foto_perfil", msg.photo[-1].file_id)
        await state.set_state(Perfil.bio)
        await msg.answer("✍️ Agora envie a <b>bio</b> (ou /pular).")

    @router.message(Perfil.bio, F.text)
    async def perfil_bio(msg: Message, state: FSMContext, db: Database):
        if msg.text != "/pular":
            await db.set_ajuste("bio", msg.text.strip()[:700])
        await state.clear()
        await msg.answer("✅ Perfil atualizado. Mande /start para ver como os fãs veem.")

    # ── Backup ─────────────────────────────────────────────────────────────
    @router.message(Command("backup"))
    async def backup(msg: Message, db: Database, bot: Bot):
        await enviar_backup(bot, db, cfg, destinos=[msg.from_user.id])

    # ── Reenvio manual ─────────────────────────────────────────────────────
    @router.message(Command("reenviar"))
    async def reenviar(msg: Message, command: CommandObject, db: Database, bot: Bot):
        partes = (command.args or "").split()
        if len(partes) != 2 or not partes[0].isdigit() or not partes[1].startswith("pack_"):
            await msg.answer("Use: <code>/reenviar USER_ID pack_xxxx</code>")
            return
        user_id, pack_id = int(partes[0]), partes[1]
        pack = await db.get_pack(pack_id)
        if not pack:
            await msg.answer("Pack não encontrado.")
            return
        try:
            await db.liberar_acesso(user_id, [pack_id], None)
            await entregar_pack(bot, user_id, pack)
            await msg.answer(f"✅ <b>{html.escape(pack.titulo)}</b> reenviado para <code>{user_id}</code>.")
        except Exception as e:
            await msg.answer(f"⚠️ Não consegui enviar: <code>{html.escape(str(e))}</code>")

    return router
