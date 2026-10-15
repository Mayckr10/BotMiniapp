"""
Fluxo do cliente:
  Canal (botão 🔓) -> t.me/SeuBot?start=pack_8f3a -> /start pack_8f3a
  -> confirmação 18+ (só na primeira vez)
  -> bot gera a cobrança PIX (QR Code + Copia e Cola) e registra a venda como PENDENTE.
A entrega acontece em webhook.py quando o gateway confirma o pagamento.

Formatos de link aceitos (o Telegram só permite A-Z a-z 0-9 _ - no parâmetro):
  ?start=pack_8f3a              pack avulso
  ?start=combo_1a2b             combo de packs
  ?start=plano_1                assinatura VIP (plano 1)
  ?start=assinar                lista os planos VIP
  ?start=pack_8f3a_CUPOM10      qualquer item + cupom

Também aqui: perfil da criadora (/start), catálogo, gorjetas e o chat com a criadora.
"""
import html
import logging
import re
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, User, WebAppInfo)

from admin_handlers import _resumo_midias, legenda_canal, parse_preco
from assinaturas import data_local, teclado_planos
from config import Config
from database import STATUS_EXPIRADO, Combo, Database, Gorjeta, Pack, Plano, fmt_reais
from payments import MockPixGateway, PixGateway
from remarketing import teclado_novo_pix
from loja import url_loja
from vendas import criar_pedido
from webhook import entregar_pack, entregar_packs, processar_pagamento

log = logging.getLogger(__name__)
router = Router(name="cliente")
router.message.filter(F.chat.type == "private")

RE_PAYLOAD = re.compile(r"^((?:pack|combo)_[0-9a-f]{4,}|plano_\d+|gorjeta_\d+)(?:_([A-Za-z0-9]{2,30}))?$")


class GorjetaValor(StatesGroup):
    valor = State()


# ════════════════════════════════════════════════════════════════════════════
#  Confirmação de maioridade (18+)
# ════════════════════════════════════════════════════════════════════════════
async def precisa_confirmar_idade(user: User, db: Database, cfg: Config) -> str | None:
    """None = pode comprar; 'pedir' = mostrar a confirmação; 'menor' = bloqueado."""
    if not cfg.exigir_maioridade or user.id in cfg.admin_ids:
        return None
    status = await db.status_idade(user.id)
    if status == "ok":
        return None
    return "menor" if status == "menor" else "pedir"


async def pedir_idade(msg: Message, payload: str, cfg: Config) -> None:
    termos = f'\n📄 <a href="{html.escape(cfg.termos_url)}">Termos de uso</a>' if cfg.termos_url else ""
    teclado = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tenho 18 anos ou mais", callback_data=f"idade:ok:{payload}")],
        [InlineKeyboardButton(text="❌ Sou menor de 18", callback_data="idade:menor")],
    ])
    await msg.answer(
        "🔞 <b>Conteúdo para maiores de 18 anos</b>\n\n"
        "Para continuar, confirme que você tem <b>18 anos ou mais</b> e que concorda em não "
        "compartilhar o conteúdo comprado." + termos,
        reply_markup=teclado,
    )


MSG_MENOR = "🚫 Este conteúdo é exclusivo para maiores de 18 anos. Não é possível comprar por aqui."


@router.callback_query(F.data.startswith("idade:"))
async def responder_idade(cb: CallbackQuery, bot: Bot, db: Database, gateway: PixGateway, cfg: Config):
    _, resposta, *resto = cb.data.split(":", 2)
    await db.registrar_usuario(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
    await cb.message.edit_reply_markup(reply_markup=None)
    if resposta != "ok":
        await db.registrar_idade(cb.from_user.id, maior=False)
        await cb.answer()
        await cb.message.answer(MSG_MENOR)
        return
    await db.registrar_idade(cb.from_user.id, maior=True)
    await cb.answer("Obrigado! ✅")
    payload = resto[0] if resto else ""
    m = RE_PAYLOAD.match(payload)
    if m:
        await iniciar_compra(cb.message, cb.from_user, m.group(1), m.group(2), bot, db, gateway, cfg)
    else:
        await cb.message.answer("✅ Confirmado! Escolha um conteúdo no Canal de Amostras.")


# ════════════════════════════════════════════════════════════════════════════
#  Compra
# ════════════════════════════════════════════════════════════════════════════
async def iniciar_compra(msg: Message, user: User, item_id: str, codigo_cupom: str | None,
                         bot: Bot, db: Database, gateway: PixGateway, cfg: Config) -> None:
    """Gera o PIX de um item e mostra no chat. `msg` é só onde responder (privado do cliente)."""
    pedido = await criar_pedido(db, gateway, cfg, user.id, item_id, codigo_cupom)
    if pedido.status == "inexistente":
        await msg.answer("😕 Esse item não existe mais ou foi removido.")
        return
    if pedido.status == "ja_comprou":
        await msg.answer("✅ Você já comprou este conteúdo! Reenviando…")
        await entregar_packs(bot, user.id, pedido.packs, pedido.item.titulo)
        return
    if pedido.status == "erro_gateway":
        await msg.answer("⚠️ Não consegui gerar o PIX agora. Tente novamente em instantes.")
        return

    item, packs, preco, cobranca, expira = pedido.item, pedido.packs, pedido.preco, pedido.cobranca, pedido.expira
    aviso_cupom = ""
    if pedido.cupom:
        aviso_cupom = (f"🏷️ Cupom <b>{pedido.cupom.codigo}</b> ({pedido.cupom.descricao}): "
                       f"de <s>{item.preco_fmt}</s> por <b>{fmt_reais(preco)}</b>\n")
    elif pedido.cupom_invalido:
        aviso_cupom = f"⚠️ Cupom <b>{html.escape(pedido.cupom_invalido)}</b> não aplicado: {pedido.motivo_cupom}.\n"

    midias = [m for p in packs for m in p.originais]
    if isinstance(item, Plano):
        assinatura = await db.get_assinatura(user.id)
        resumo = "🔐 Acesso ao canal VIP com todo o conteúdo exclusivo\n"
        if assinatura and assinatura.ativa:
            resumo += f"➕ Você já é assinante até {data_local(assinatura.expira_em, cfg)}: os dias serão somados\n"
    elif isinstance(item, Gorjeta):
        resumo = f"💝 Um mimo para {html.escape(cfg.criadora_nome)}\n"
    elif isinstance(item, Combo):
        soma = sum(p.preco for p in packs)
        resumo = (f"🎁 Combo com {len(packs)} packs · {_resumo_midias(midias)}\n"
                  + "".join(f"  • {html.escape(p.titulo)}\n" for p in packs)
                  + (f"💸 Separados sairiam por <s>{fmt_reais(soma)}</s>\n" if soma > item.preco else ""))
    else:
        resumo = f"{_resumo_midias(midias)} · sem desfoque\n"

    hora = expira.astimezone(ZoneInfo(cfg.fuso_horario)).strftime("%H:%M")
    legenda = (
        f"🔥 <b>{html.escape(item.titulo)}</b>\n"
        f"{resumo}"
        f"{aviso_cupom}"
        f"💰 Valor: <b>{fmt_reais(preco)}</b>\n\n"
        "📲 Pague com o QR Code acima ou com o <b>PIX Copia e Cola</b> (toque para copiar):\n\n"
        f"<code>{cobranca.copia_e_cola}</code>\n\n"
        f"⌛ Válido até {hora}. Assim que o pagamento for confirmado, o conteúdo chega aqui automaticamente."
    )
    teclado = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Já paguei", callback_data=f"pix:{cobranca.pix_id}")
    ]] + teclado_teste(gateway, cobranca.pix_id))
    await msg.answer_photo(
        BufferedInputFile(cobranca.qr_png, filename="pix.png"), caption=legenda, reply_markup=teclado
    )


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(RE_PAYLOAD)))
async def start_com_item(msg: Message, command: CommandObject, bot: Bot, db: Database,
                         gateway: PixGateway, cfg: Config):
    item_id, codigo_cupom = RE_PAYLOAD.match(command.args).groups()
    user = msg.from_user
    await db.registrar_usuario(user.id, user.full_name, user.username)
    await db.registrar_clique(user.id, item_id)          # para a taxa de conversão do /stats

    idade = await precisa_confirmar_idade(user, db, cfg)
    if idade == "menor":
        await msg.answer(MSG_MENOR)
        return
    if idade == "pedir":
        await pedir_idade(msg, command.args, cfg)
        return
    await iniciar_compra(msg, user, item_id, codigo_cupom, bot, db, gateway, cfg)


async def comprar(msg: Message, user: User, payload: str, bot: Bot, db: Database,
                  gateway: PixGateway, cfg: Config) -> None:
    """Ponto único de compra a partir de botões: confere 18+ e gera o PIX."""
    m = RE_PAYLOAD.match(payload)
    if not m:
        return
    idade = await precisa_confirmar_idade(user, db, cfg)
    if idade == "menor":
        await msg.answer(MSG_MENOR)
    elif idade == "pedir":
        await pedir_idade(msg, payload, cfg)
    else:
        await iniciar_compra(msg, user, m.group(1), m.group(2), bot, db, gateway, cfg)


@router.callback_query(F.data.startswith("comprar:"))
async def comprar_cb(cb: CallbackQuery, bot: Bot, db: Database, gateway: PixGateway, cfg: Config):
    await cb.answer()
    await db.registrar_usuario(cb.from_user.id, cb.from_user.full_name, cb.from_user.username)
    payload = cb.data.split(":", 1)[1]
    await db.registrar_clique(cb.from_user.id, RE_PAYLOAD.match(payload).group(1) if RE_PAYLOAD.match(payload) else payload)
    await comprar(cb.message, cb.from_user, payload, bot, db, gateway, cfg)


# ════════════════════════════════════════════════════════════════════════════
#  Perfil da criadora (/start sem parâmetro) e menus
# ════════════════════════════════════════════════════════════════════════════
def teclado_perfil(cfg: Config) -> InlineKeyboardMarkup:
    url = url_loja(cfg)
    loja = [[InlineKeyboardButton(text="🛍️ Abrir a loja", web_app=WebAppInfo(url=url))]] if url else []
    return InlineKeyboardMarkup(inline_keyboard=loja + [
        [InlineKeyboardButton(text="⭐ Assinar VIP", callback_data="menu:planos")],
        [InlineKeyboardButton(text="🛍️ Packs", callback_data="menu:packs"),
         InlineKeyboardButton(text="💝 Gorjeta", callback_data="menu:gorjeta")],
        [InlineKeyboardButton(text="🗂️ Minhas compras", callback_data="menu:meus")],
    ])


async def mostrar_perfil(msg: Message, user: User, db: Database, cfg: Config) -> None:
    bio = await db.get_ajuste("bio", "Conteúdo exclusivo todos os dias. Assine o VIP ou compre packs avulsos 🔥")
    foto = await db.get_ajuste("foto_perfil")
    planos = await db.listar_planos()
    packs = await db.listar_packs()
    partes = []
    if planos:
        partes.append(f"⭐ VIP a partir de {min(planos, key=lambda p: p.preco).preco_fmt}")
    if packs:
        partes.append(f"📦 {len(packs)} pack{'s' if len(packs) > 1 else ''}")
    assinatura = await db.get_assinatura(user.id)
    status = (f"\n\n✅ Você é assinante VIP até {data_local(assinatura.expira_em, cfg)}"
              if assinatura and assinatura.ativa else "")
    texto = (f"✨ <b>{html.escape(cfg.criadora_nome)}</b>\n\n{html.escape(bio)}\n\n" + " · ".join(partes) + status)
    if user.id in cfg.admin_ids:
        texto += "\n\n👑 Você é admin: /admin abre o painel."
    if foto:
        await msg.answer_photo(foto, caption=texto, reply_markup=teclado_perfil(cfg))
    else:
        await msg.answer(texto, reply_markup=teclado_perfil(cfg))


@router.message(CommandStart(deep_link=True, magic=F.args == "assinar"))
@router.message(Command("assinar"))
async def assinar(msg: Message, db: Database, cfg: Config):
    await db.registrar_usuario(msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
    await mostrar_planos(msg, msg.from_user, db, cfg)


async def mostrar_planos(msg: Message, user: User, db: Database, cfg: Config) -> None:
    planos = await db.listar_planos()
    if not planos:
        await msg.answer("O VIP ainda não está aberto. Volte em breve 💜")
        return
    assinatura = await db.get_assinatura(user.id)
    extra = (f"\n\n✅ Você já é assinante até {data_local(assinatura.expira_em, cfg)}. Renovando agora, os dias são somados."
             if assinatura and assinatura.ativa else "")
    await msg.answer(
        f"⭐ <b>VIP de {html.escape(cfg.criadora_nome)}</b>\n\n"
        "🔐 Acesso ao canal privado com todo o conteúdo exclusivo\n"
        "🔄 Sem renovação automática: você renova quando quiser\n\nEscolha seu plano:" + extra,
        reply_markup=teclado_planos(planos),
    )


@router.message(Command("assinatura"))
async def minha_assinatura(msg: Message, db: Database, cfg: Config):
    a = await db.get_assinatura(msg.from_user.id)
    if a and a.ativa:
        await msg.answer(f"⭐ Sua assinatura VIP vale até {data_local(a.expira_em, cfg)}.\nQuer renovar? Os dias são somados:",
                         reply_markup=teclado_planos(await db.listar_planos()))
    else:
        await mostrar_planos(msg, msg.from_user, db, cfg)


@router.message(Command("gorjeta"))
async def gorjeta_cmd(msg: Message, cfg: Config):
    await mostrar_gorjetas(msg, cfg)


async def mostrar_gorjetas(msg: Message, cfg: Config) -> None:
    valores = [int(v) for v in cfg.gorjeta_valores.split(",") if v.strip().isdigit()]
    botoes = [InlineKeyboardButton(text=f"R$ {v}", callback_data=f"comprar:gorjeta_{v * 100}") for v in valores]
    linhas = [botoes[i:i + 2] for i in range(0, len(botoes), 2)]
    linhas.append([InlineKeyboardButton(text="✏️ Outro valor", callback_data="gorjeta:outro")])
    await msg.answer(f"💝 Mande um mimo para {html.escape(cfg.criadora_nome)}:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=linhas))


@router.callback_query(F.data == "gorjeta:outro")
async def gorjeta_outro(cb: CallbackQuery, state: FSMContext):
    await state.set_state(GorjetaValor.valor)
    await cb.answer()
    await cb.message.answer("✏️ Quanto você quer enviar? (ex: <code>15,00</code>)")


@router.message(GorjetaValor.valor, F.text)
async def gorjeta_valor(msg: Message, state: FSMContext, bot: Bot, db: Database, gateway: PixGateway, cfg: Config):
    valor = parse_preco(msg.text)
    if valor is None:
        await msg.answer("⚠️ Valor inválido. Envie algo como <code>15,00</code> (mínimo R$ 1,00).")
        return
    await state.clear()
    await comprar(msg, msg.from_user, f"gorjeta_{valor}", bot, db, gateway, cfg)


@router.callback_query(F.data.startswith("menu:"))
async def menu(cb: CallbackQuery, db: Database, cfg: Config):
    opcao = cb.data.split(":", 1)[1]
    await cb.answer()
    if opcao == "planos":
        await mostrar_planos(cb.message, cb.from_user, db, cfg)
    elif opcao == "gorjeta":
        await mostrar_gorjetas(cb.message, cfg)
    elif opcao == "meus":
        await meus_packs_de(cb.message, cb.from_user.id, db)
    elif opcao == "packs":
        packs, combos = await db.listar_packs(), await db.listar_combos()
        if not packs and not combos:
            await cb.message.answer("Ainda não há packs à venda. Que tal o VIP? ⭐")
            return
        linhas = [[InlineKeyboardButton(text=f"🎁 {c.titulo} · {c.preco_fmt}", callback_data=f"ver:{c.id}")] for c in combos]
        linhas += [[InlineKeyboardButton(text=f"📦 {p.titulo} · {p.preco_fmt}", callback_data=f"ver:{p.id}")] for p in packs]
        await cb.message.answer("🛍️ <b>Packs disponíveis</b>\nToque para ver a prévia:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=linhas))


@router.callback_query(F.data.startswith("ver:"))
async def ver_item(cb: CallbackQuery, db: Database):
    """Prévia embaçada de um pack/combo do catálogo, com botão de compra."""
    item_id = cb.data.split(":", 1)[1]
    item = await db.get_item(item_id)
    packs = await db.packs_do_item(item_id)
    await cb.answer()
    if not item or not packs:
        await cb.message.answer("Esse pack não está mais disponível.")
        return
    teclado = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔓 Liberar · {item.preco_fmt}", callback_data=f"comprar:{item_id}")
    ]])
    capa = next((p.blur[0]["file_id"] for p in packs if p.blur), None)
    legenda = legenda_canal(item) if isinstance(item, Pack) else (
        f"🎁 <b>{html.escape(item.titulo)}</b>\n" + "".join(f"• {html.escape(p.titulo)}\n" for p in packs)
        + f"\n💰 <b>{item.preco_fmt}</b>")
    if capa:
        await cb.message.answer_photo(capa, caption=legenda, reply_markup=teclado)
    else:
        await cb.message.answer(legenda, reply_markup=teclado)


@router.message(CommandStart())
async def start_simples(msg: Message, cfg: Config, db: Database):
    user = msg.from_user
    await db.registrar_usuario(user.id, user.full_name, user.username)
    await mostrar_perfil(msg, user, db, cfg)


@router.message(Command("meus_packs"))
async def meus_packs(msg: Message, db: Database):
    await meus_packs_de(msg, msg.from_user.id, db)


async def meus_packs_de(msg: Message, user_id: int, db: Database) -> None:
    packs = await db.packs_do_cliente(user_id)
    if not packs:
        await msg.answer("Você ainda não comprou nenhum pack. Confira o Canal de Amostras 😉")
        return
    teclado = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📦 {p.titulo}", callback_data=f"rever:{p.id}")] for p in packs
    ])
    await msg.answer("🗂️ <b>Seus packs</b>\nToque para receber de novo:", reply_markup=teclado)


@router.callback_query(F.data.startswith("rever:"))
async def rever(cb: CallbackQuery, bot: Bot, db: Database):
    pack_id = cb.data.split(":", 1)[1]
    pack = await db.get_pack(pack_id)
    if not pack or not await db.ja_comprou(cb.from_user.id, pack_id):
        await cb.answer("Pack indisponível.", show_alert=True)
        return
    await cb.answer("Enviando…")
    await entregar_pack(bot, cb.from_user.id, pack)


@router.callback_query(F.data.startswith("novopix:"))
async def novo_pix(cb: CallbackQuery, bot: Bot, db: Database, gateway: PixGateway, cfg: Config):
    """Gera um PIX novo para o mesmo item (e o mesmo cupom, se ainda for válido)."""
    venda = await db.get_venda(int(cb.data.split(":", 1)[1]))
    if not venda or venda.user_id != cb.from_user.id:
        await cb.answer("Cobrança não encontrada.", show_alert=True)
        return
    await cb.answer("Gerando novo PIX…")
    await cb.message.edit_reply_markup(reply_markup=None)
    await iniciar_compra(cb.message, cb.from_user, venda.pack_id, venda.cupom, bot, db, gateway, cfg)


def teclado_teste(gateway: PixGateway, pix_id: str) -> list:
    """Só com PIX_GATEWAY=mock: botão para simular o pagamento direto no Telegram (testar pelo celular)."""
    if not isinstance(gateway, MockPixGateway):
        return []
    return [[InlineKeyboardButton(text="🧪 Simular pagamento (teste)", callback_data=f"testepix:{pix_id}")]]


@router.callback_query(F.data.startswith("testepix:"))
async def simular_pagamento(cb: CallbackQuery, bot: Bot, db: Database, gateway: PixGateway, cfg: Config):
    pix_id = cb.data.split(":", 1)[1]
    if not isinstance(gateway, MockPixGateway) or not gateway.marcar_pago_teste(pix_id):
        await cb.answer("Cobrança de teste não encontrada (o bot reiniciou?). Gere um novo PIX.", show_alert=True)
        return
    await ja_paguei(cb, bot, db, gateway, cfg)


@router.callback_query(F.data.startswith("pix:"))
async def ja_paguei(cb: CallbackQuery, bot: Bot, db: Database, gateway: PixGateway, cfg: Config):
    """Botão 'Já paguei': consulta o gateway na hora (caso o webhook atrase)."""
    pix_id = cb.data.split(":", 1)[1]
    resultado = await processar_pagamento(bot, db, gateway, pix_id, cfg)
    if resultado == "nao_pago":
        venda = await db.get_venda_por_pix(pix_id)
        if venda and venda.status == STATUS_EXPIRADO:
            await cb.answer("⌛ Este PIX expirou e não encontramos o pagamento. Gere um novo código.", show_alert=True)
            await cb.message.answer("Toque abaixo para gerar um novo PIX:", reply_markup=teclado_novo_pix(venda.id))
            return
    textos = {
        "entregue": "✅ Pagamento confirmado!",
        "ja_processado": "Esse pagamento já foi confirmado e entregue 😉",
        "nao_pago": "⏳ Ainda não identificamos o pagamento. Aguarde alguns segundos e tente de novo.",
    }
    await cb.answer(textos.get(resultado, "Cobrança não encontrada."), show_alert=resultado == "nao_pago")


# ════════════════════════════════════════════════════════════════════════════
#  Mensagens livres (incluído por último): o bot não tem chat, então mostra o menu
# ════════════════════════════════════════════════════════════════════════════
fallback_router = Router(name="fallback_cliente")
fallback_router.message.filter(F.chat.type == "private")


@fallback_router.message(StateFilter(None), ~F.text.startswith("/"))
async def mensagem_livre(msg: Message, cfg: Config):
    if msg.from_user.id in cfg.admin_ids:
        await msg.reply("Comandos da criadora: /admin")
        return
    await msg.answer("Use os botões abaixo para ver os conteúdos 👇", reply_markup=teclado_perfil(cfg))
