"""
/admin no chat do bot: painel todo por botões inline.

Cada botão roda o mesmo comando que já existia (/stats, /packs, /novo_pack...), então não há duas lógicas
para manter. Comandos que pedem dados (ex.: /dar_vip USER_ID dias) perguntam no chat e rodam com a resposta.
O menu é editado no lugar (uma mensagem só, com "‹ Voltar"), para não encher a conversa.
"""
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity,
                           Update, WebAppInfo)

from config import Config

DP: Dispatcher | None = None            # main.py preenche: os botões reaproveitam os comandos pelo dispatcher

# (rótulo, comando, pergunta) — sem pergunta, roda direto; com pergunta, espera a resposta no chat
SECOES: dict[str, tuple[str, list[tuple[str, str, str]]]] = {
    "conteudo": ("📦 Conteúdo", [
        ("➕ Novo pack", "/novo_pack", ""),
        ("✉️ Mensagem paga (PPV)", "/ppv", ""),
        ("📦 Meus packs", "/packs", ""),
        ("🎁 Novo combo", "/novo_combo", ""),
        ("🗂️ Combos", "/combos", ""),
    ]),
    "vip": ("⭐ Assinatura VIP", [
        ("⭐ Planos", "/planos", ""),
        ("➕ Novo plano", "/plano", "Mande <b>nome, dias e preço</b>.\nEx: <code>Trimestral 90 79,90</code>"),
        ("🗑️ Desativar plano", "/plano_off", "Mande o <b>id do plano</b> (aparece em ⭐ Planos)."),
        ("👥 Assinantes", "/assinantes", ""),
        ("🎟️ Dar VIP", "/dar_vip", "Mande o <b>ID do fã e os dias</b>.\nEx: <code>123456789 30</code>"),
        ("❌ Remover VIP", "/remover_vip", "Mande o <b>ID do fã</b>."),
    ]),
    "vendas": ("💰 Vendas", [
        ("📊 Estatísticas", "/stats", ""),
        ("🏷️ Cupons ativos", "/cupons", ""),
        ("➕ Novo cupom", "/cupom", "Mande o <b>código e o desconto</b>.\nEx: <code>VERAO10 10%</code> ou <code>FIXO 5,00</code>"),
        ("🗑️ Desativar cupom", "/cupom_off", "Mande o <b>código</b> do cupom."),
        ("📣 Mensagem para compradores", "/broadcast", ""),
        ("🔁 Reenviar um pack", "/reenviar", "Mande o <b>ID do fã e o pack</b>.\nEx: <code>123456789 pack_ab12</code>"),
    ]),
    "protecao": ("🛡️ Proteção", [
        ("🚫 Bloqueados", "/bloqueados", ""),
        ("⛔ Bloquear fã", "/bloquear", "Mande o <b>ID do fã</b> (aparece nos avisos de venda)."),
        ("✅ Desbloquear fã", "/desbloquear", "Mande o <b>ID do fã</b>."),
        ("💾 Backup agora", "/backup", ""),
    ]),
    "conta": ("👤 Perfil e conta", [
        ("🖼️ Foto e bio", "/perfil", ""),
        ("📋 Lista de comandos", "/ajuda", ""),
    ]),
}
DONO = [("✉️ Convidar criadora", "/convite", ""), ("✂️ Split", "/split", "")]


class Pergunta(StatesGroup):
    resposta = State()


def _botao(texto: str, dados: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=texto, callback_data=dados)


def _principal(cfg: Config, user_id: int) -> InlineKeyboardMarkup:
    from loja import url_loja
    linhas, chaves = [], list(SECOES)
    for i in range(0, len(chaves), 2):
        linhas.append([_botao(SECOES[k][0], f"adm:s:{k}") for k in chaves[i:i + 2]])
    if user_id in cfg.dono_ids:
        linhas.append([_botao("🏛️ Plataforma (dono)", "adm:s:dono")])
    url = url_loja(cfg)
    if url:
        linhas.append([InlineKeyboardButton(text="👑 Abrir painel completo", web_app=WebAppInfo(url=url))])
    return InlineKeyboardMarkup(inline_keyboard=linhas)


def _itens(secao: str) -> list[tuple[str, str, str]]:
    return DONO if secao == "dono" else SECOES[secao][1]


def _secao(secao: str) -> InlineKeyboardMarkup:
    itens = _itens(secao)
    linhas = []
    for i in range(0, len(itens), 2):
        linhas.append([_botao(r, f"adm:c:{secao}:{n}") for n, (r, _, _) in enumerate(itens[i:i + 2], start=i)])
    linhas.append([_botao("‹ Voltar", "adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=linhas)


TOPO = "👑 <b>Painel da criadora</b>\nEscolha uma área:"


async def rodar_comando(bot: Bot, origem: Message, user, texto: str) -> None:
    """Roda `texto` (ex.: "/dar_vip 123 30") como se a pessoa tivesse digitado, pelos mesmos handlers."""
    nome = texto.split()[0]
    msg = origem.model_copy(update={
        "text": texto, "from_user": user, "caption": None, "photo": None, "reply_markup": None,
        "reply_to_message": None, "entities": [MessageEntity(type="bot_command", offset=0, length=len(nome))],
    }).as_(bot)
    await DP.feed_update(bot, Update(update_id=0, message=msg))


def get_router(cfg: Config) -> Router:
    router = Router(name="admin_menu")
    # admin_ids muda quando uma criadora vincula a conta, por isso o teste é feito na hora
    router.message.filter(F.chat.type == "private",
                          lambda m: bool(m.from_user) and (m.from_user.id in cfg.admin_ids or m.from_user.id in cfg.dono_ids))
    router.callback_query.filter(F.data.startswith("adm:"))

    def pode(user_id: int, secao: str | None = None) -> bool:
        return user_id in (cfg.dono_ids if secao == "dono" else cfg.admin_ids)

    @router.message(Command("admin"))
    async def menu(msg: Message, state: FSMContext):
        if msg.from_user.id not in cfg.admin_ids and msg.from_user.id in cfg.dono_ids:
            await msg.answer("🏛️ <b>Plataforma</b>", reply_markup=_secao("dono"))
            return
        if msg.from_user.id not in cfg.admin_ids:
            return
        await state.clear()
        await msg.answer(TOPO, reply_markup=_principal(cfg, msg.from_user.id))

    @router.callback_query(F.data == "adm:menu")
    async def voltar(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        if not pode(cb.from_user.id):
            return await cb.answer()
        await cb.message.edit_text(TOPO, reply_markup=_principal(cfg, cb.from_user.id))
        await cb.answer()

    @router.callback_query(F.data.startswith("adm:s:"))
    async def abrir_secao(cb: CallbackQuery, state: FSMContext):
        secao = cb.data.split(":")[2]
        if (secao != "dono" and secao not in SECOES) or not pode(cb.from_user.id, secao):
            return await cb.answer()
        await state.clear()
        titulo = "🏛️ <b>Plataforma</b>" if secao == "dono" else f"<b>{SECOES[secao][0]}</b>"
        await cb.message.edit_text(titulo, reply_markup=_secao(secao))
        await cb.answer()

    @router.callback_query(F.data.startswith("adm:c:"))
    async def comando(cb: CallbackQuery, state: FSMContext, bot: Bot):
        _, _, secao, n = cb.data.split(":")
        try:
            rotulo, cmd, pergunta = _itens(secao)[int(n)]
        except (KeyError, IndexError, ValueError):
            return await cb.answer()
        if not pode(cb.from_user.id, secao):
            return await cb.answer()
        await cb.answer()
        if pergunta:
            await state.set_state(Pergunta.resposta)
            await state.update_data(cmd=cmd)
            await cb.message.answer(f"{rotulo}\n\n{pergunta}",
                                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_botao("Cancelar", "adm:cancelar")]]))
            return
        await state.clear()
        await rodar_comando(bot, cb.message, cb.from_user, cmd)

    @router.callback_query(F.data == "adm:cancelar")
    async def cancelar(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text("❌ Cancelado.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_botao("‹ Painel", "adm:menu")]]))
        await cb.answer()

    @router.message(StateFilter(Pergunta.resposta), F.text, ~F.text.startswith("/"))
    async def resposta(msg: Message, state: FSMContext, bot: Bot):
        cmd = (await state.get_data()).get("cmd", "")
        await state.clear()
        await rodar_comando(bot, msg, msg.from_user, f"{cmd} {msg.text.strip()}")
        await msg.answer("O que mais?", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_botao("‹ Painel", "adm:menu")]]))

    return router
