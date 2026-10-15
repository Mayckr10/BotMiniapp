"""
Painel administrativo da criadora (chat privado com o bot).

/ppv usa o mesmo fluxo, mas em vez de postar no canal envia a prévia embaçada no
PRIVADO dos assinantes (ou de todos os clientes), com o botão de desbloqueio: o
"pay per view" do Privacy.

Fluxo FSM do /novo_pack:
  1. NovoPack.midias   -> recebe fotos/vídeos (pode ser álbum) até tocar em "Concluir"
                          → gera as amostras embaçadas (blur_service)
  2. NovoPack.titulo   -> recebe o título
  3. NovoPack.preco    -> recebe o preço (ex: 29,90)
  4. NovoPack.confirmar-> botão "Publicar" salva no banco e posta no Canal de Amostras
"""
import asyncio
import html
import logging
import re
from collections import defaultdict

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message)
from aiogram.utils.deep_linking import create_start_link

from blur_service import blur_foto_telegram, teaser_video_telegram
from config import Config
from database import Database, Pack

log = logging.getLogger(__name__)

# Um lock por admin: álbuns chegam como várias mensagens simultâneas e
# sem isso duas fotos poderiam sobrescrever a lista uma da outra no FSM.
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class NovoPack(StatesGroup):
    midias = State()
    titulo = State()
    preco = State()
    confirmar = State()


def _kb(*linhas: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in linha] for linha in linhas
    ])


KB_CONCLUIR = _kb([("✅ Concluir envio", "np:concluir")], [("✖️ Cancelar", "np:cancelar")])
KB_CONFIRMAR = _kb([("📢 Publicar no canal", "np:publicar")], [("✖️ Cancelar", "np:cancelar")])


def parse_preco(texto: str) -> int | None:
    """'R$ 29,90' | '29.90' | '30' -> 2990 | 2990 | 3000 (centavos)."""
    t = re.sub(r"[^\d,\.]", "", texto)
    if not t:
        return None
    if "," in t:                      # formato BR: 1.299,90
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):   # "1.000" ou "1.500.000": ponto de milhar, não decimal
        t = t.replace(".", "")
    try:
        centavos = round(float(t) * 100)
    except ValueError:
        return None
    return centavos if centavos >= 100 else None   # mínimo R$ 1,00


def _resumo_midias(midias: list[dict]) -> str:
    fotos = sum(m["tipo"] == "photo" for m in midias)
    videos = sum(m["tipo"] == "video" for m in midias)
    partes = []
    if fotos:
        partes.append(f"📸 {fotos} foto{'s' if fotos > 1 else ''}")
    if videos:
        partes.append(f"🎬 {videos} vídeo{'s' if videos > 1 else ''}")
    return " · ".join(partes)


def legenda_canal(pack: Pack) -> str:
    return (
        f"🔥 <b>{html.escape(pack.titulo)}</b>\n\n"
        f"{_resumo_midias(pack.originais)}\n"
        f"💰 <b>{pack.preco_fmt}</b>\n\n"
        f"👇 Toque no botão abaixo para liberar o conteúdo completo, sem desfoque."
    )


async def postar_no_canal(bot: Bot, cfg: Config, pack: Pack) -> None:
    """Publica as amostras embaçadas no Canal + botão com deep link para o bot."""
    link = await create_start_link(bot, pack.id)          # https://t.me/SeuBot?start=pack_8f3a
    teclado = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔓 Liberar Conteúdo - {pack.preco_fmt}", url=link)
    ]])
    blur = pack.blur[:10]
    if len(blur) == 1:
        await bot.send_photo(cfg.channel_id, blur[0]["file_id"], caption=legenda_canal(pack), reply_markup=teclado)
    else:
        # Álbuns não aceitam botões inline: manda o álbum e, em seguida, a mensagem com o botão.
        await bot.send_media_group(cfg.channel_id, [InputMediaPhoto(media=m["file_id"]) for m in blur])
        await bot.send_message(cfg.channel_id, legenda_canal(pack), reply_markup=teclado)


def get_router(cfg: Config) -> Router:
    router = Router(name="admin")
    # Tudo neste router só vale para admins, em chat privado.
    router.message.filter(F.chat.type == "private", F.from_user.id.in_(cfg.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    # ── Ajuda / cancelar ───────────────────────────────────────────────────
    @router.message(Command("admin", "ajuda"))
    async def ajuda(msg: Message):
        await msg.answer(
            "👑 <b>Painel da criadora</b>\n\n"
            "<b>Conteúdo</b>\n"
            "/novo_pack – criar e publicar um novo pack\n"
            "/packs – listar packs e links de venda\n"
            "/novo_combo – juntar packs num combo com desconto\n"
            "/combos – listar combos\n"
            "/ppv – mensagem paga (pay per view) no privado dos fãs\n\n"
            "<b>Assinatura VIP</b>\n"
            "/planos – planos e preços · /plano Nome dias preço\n"
            "/assinantes – resumo dos assinantes\n"
            "/dar_vip USER_ID dias – VIP de cortesia · /remover_vip USER_ID\n"
            "/perfil – foto e bio da sua página no bot\n\n"
            "<b>Vendas</b>\n"
            "/cupom – criar cupom (ex: <code>/cupom VERAO10 10%</code>)\n"
            "/cupons – cupons ativos · /cupom_off CODIGO\n"
            "/stats – vendas, ticket médio e conversão\n"
            "/broadcast – mensagem para os compradores\n"
            "/reenviar USER_ID pack_xxxx – reentrega manual\n"
            "/backup – receber o backup do banco agora\n\n"
            "💬 Mensagens dos fãs chegam aqui. <b>Responda</b> a elas para falar com a pessoa.\n"
            "/cancelar – cancelar a operação em andamento"
        )

    @router.message(Command("cancelar"))
    async def cancelar(msg: Message, state: FSMContext):
        await state.clear()
        await msg.answer("❌ Operação cancelada.")

    @router.callback_query(F.data == "np:cancelar")
    async def cancelar_cb(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("❌ Criação do pack cancelada.")
        await cb.answer()

    @router.message(Command("packs"))
    async def listar(msg: Message, db: Database, bot: Bot):
        packs = await db.listar_packs()
        if not packs:
            await msg.answer("Nenhum pack criado ainda. Use /novo_pack.")
            return
        linhas = [f"• <b>{html.escape(p.titulo)}</b> – {p.preco_fmt}\n  "
                  f"<code>{await create_start_link(bot, p.id)}</code>" for p in packs]
        await msg.answer("📦 <b>Seus packs</b>\n\n" + "\n".join(linhas))

    # ── 1. Início ──────────────────────────────────────────────────────────
    @router.message(Command("novo_pack", "ppv"))
    async def novo_pack(msg: Message, state: FSMContext, command: CommandObject):
        ppv = command.command == "ppv"
        await state.clear()
        await state.set_state(NovoPack.midias)
        await state.update_data(midias=[], status_msg_id=None, ppv=ppv)
        await msg.answer(
            ("💌 <b>Nova mensagem paga (PPV)</b>\nOs fãs recebem a prévia embaçada no privado e pagam para desbloquear.\n\n"
             if ppv else "📦 <b>Novo pack</b>\n\n") +
            "Envie as <b>fotos e vídeos originais</b> (pode mandar em álbum).\n"
            "Quando terminar, toque em <b>✅ Concluir envio</b> ou digite /pronto."
        )

    # ── 2. Recebendo mídias ────────────────────────────────────────────────
    @router.message(NovoPack.midias, F.photo | F.video)
    async def receber_midia(msg: Message, state: FSMContext, bot: Bot):
        if msg.photo:
            item = {"tipo": "photo", "file_id": msg.photo[-1].file_id}   # [-1] = maior resolução
        else:
            item = {"tipo": "video", "file_id": msg.video.file_id}
        item["ordem"] = msg.message_id      # álbuns chegam fora de ordem; usado para reordenar

        async with _locks[msg.from_user.id]:
            dados = await state.get_data()
            midias = dados["midias"] + [item]
            texto = f"📥 {len(midias)} mídia(s) recebida(s): {_resumo_midias(midias)}\nEnvie mais ou conclua."
            status_id = dados.get("status_msg_id")
            if status_id:
                try:
                    await bot.edit_message_text(texto, chat_id=msg.chat.id, message_id=status_id, reply_markup=KB_CONCLUIR)
                except Exception:
                    status_id = None
            if not status_id:
                status_id = (await msg.answer(texto, reply_markup=KB_CONCLUIR)).message_id
            await state.update_data(midias=midias, status_msg_id=status_id)

    @router.message(NovoPack.midias, Command("pronto"))
    async def pronto_cmd(msg: Message, state: FSMContext, bot: Bot, cfg: Config):
        await _concluir_midias(msg, state, bot, cfg)

    @router.callback_query(NovoPack.midias, F.data == "np:concluir")
    async def pronto_cb(cb: CallbackQuery, state: FSMContext, bot: Bot, cfg: Config):
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=None)
        await _concluir_midias(cb.message, state, bot, cfg)

    @router.message(NovoPack.midias)
    async def midia_invalida(msg: Message):
        await msg.answer("Envie fotos ou vídeos, ou /pronto para continuar.")

    async def _concluir_midias(msg: Message, state: FSMContext, bot: Bot, cfg: Config):
        async with _locks[msg.chat.id]:
            midias = sorted((await state.get_data()).get("midias", []), key=lambda m: m["ordem"])
            midias = [{"tipo": m["tipo"], "file_id": m["file_id"]} for m in midias]
            await state.update_data(midias=midias)
        if not midias:
            await msg.answer("⚠️ Você ainda não enviou nenhuma mídia.")
            return

        aguarde = await msg.answer(f"⏳ Gerando {len(midias)} amostra(s) com desfoque…")
        # Gera os teasers (Pillow / FFmpeg) em paralelo
        teasers = await asyncio.gather(*[
            blur_foto_telegram(bot, m["file_id"], cfg.blur_radius) if m["tipo"] == "photo"
            else teaser_video_telegram(bot, m["file_id"], cfg.blur_radius)
            for m in midias
        ])

        # Sobe as imagens embaçadas para o Telegram (no chat da admin, como prévia)
        # para obter os file_ids que serão reutilizados no canal sem novo upload.
        arquivos = [BufferedInputFile(t.getvalue(), filename=f"teaser_{i}.jpg") for i, t in enumerate(teasers)]
        blur_ids: list[dict] = []
        for i in range(0, len(arquivos), 10):                 # álbuns têm no máximo 10 itens
            lote = arquivos[i:i + 10]
            if len(lote) == 1:
                enviadas = [await bot.send_photo(msg.chat.id, lote[0])]
            else:
                enviadas = await bot.send_media_group(msg.chat.id, [InputMediaPhoto(media=a) for a in lote])
            blur_ids += [{"tipo": "photo", "file_id": m.photo[-1].file_id} for m in enviadas]

        await bot.delete_message(msg.chat.id, aguarde.message_id)
        await state.update_data(blur=blur_ids)
        await state.set_state(NovoPack.titulo)
        await msg.answer("👆 Prévia das amostras embaçadas.\n\n✏️ Agora envie o <b>título</b> do pack:")

    # ── 3. Título ──────────────────────────────────────────────────────────
    @router.message(NovoPack.titulo, F.text)
    async def receber_titulo(msg: Message, state: FSMContext):
        titulo = msg.text.strip()[:100]
        await state.update_data(titulo=titulo)
        await state.set_state(NovoPack.preco)
        await msg.answer("💰 Qual o <b>preço</b>? (ex: <code>29,90</code>)")

    # ── 4. Preço ───────────────────────────────────────────────────────────
    @router.message(NovoPack.preco, F.text)
    async def receber_preco(msg: Message, state: FSMContext, db: Database):
        preco = parse_preco(msg.text)
        if preco is None:
            await msg.answer("⚠️ Preço inválido. Envie algo como <code>29,90</code> (mínimo R$ 1,00).")
            return
        await state.update_data(preco=preco)
        await state.set_state(NovoPack.confirmar)
        d = await state.get_data()
        previa = Pack(id="—", titulo=d["titulo"], preco=preco, originais=d["midias"], blur=d["blur"])
        await msg.answer(
            "📝 <b>Confira o pack:</b>\n\n"
            f"Título: <b>{html.escape(previa.titulo)}</b>\n"
            f"Preço: <b>{previa.preco_fmt}</b>\n"
            f"Conteúdo: {_resumo_midias(previa.originais)}\n\n"
            + ("Para quem enviar a mensagem paga?" if d.get("ppv") else "Publicar no Canal de Amostras?"),
            reply_markup=KB_CONFIRMAR if not d.get("ppv") else _kb(
                [(f"⭐ Assinantes ({len(await db.assinantes_ativos())})", "np:ppv:assinantes")],
                [(f"👥 Todos os clientes ({len(await db.todos_clientes())})", "np:ppv:todos")],
                [("✖️ Cancelar", "np:cancelar")]),
        )

    # ── 5. Publicar ────────────────────────────────────────────────────────
    @router.callback_query(NovoPack.confirmar, F.data == "np:publicar")
    async def publicar(cb: CallbackQuery, state: FSMContext, bot: Bot, db: Database, cfg: Config):
        await cb.answer("Publicando…")
        d = await state.get_data()
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)

        pack_id = await db.criar_pack(d["titulo"], d["preco"], d["midias"], d["blur"])
        pack = await db.get_pack(pack_id)
        link = await create_start_link(bot, pack_id)
        try:
            await postar_no_canal(bot, cfg, pack)
            await cb.message.answer(f"✅ Pack <code>{pack_id}</code> publicado no canal!\n🔗 Link de venda: {link}")
        except Exception as e:  # ex: bot não é admin do canal
            log.exception("Falha ao postar no canal")
            await cb.message.answer(
                f"⚠️ Pack <code>{pack_id}</code> salvo, mas não consegui postar no canal:\n"
                f"<code>{html.escape(str(e))}</code>\n\nConfira se o bot é admin do canal.\n🔗 Link: {link}"
            )

    @router.callback_query(NovoPack.confirmar, F.data.startswith("np:ppv:"))
    async def enviar_ppv(cb: CallbackQuery, state: FSMContext, bot: Bot, db: Database, cfg: Config):
        publico = cb.data.rsplit(":", 1)[1]
        d = await state.get_data()
        await state.clear()
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer("Enviando…")
        pack_id = await db.criar_pack(d["titulo"], d["preco"], d["midias"], d["blur"])
        await db.definir_tipo_pack(pack_id, "ppv")
        pack = await db.get_pack(pack_id)
        destinos = await (db.assinantes_ativos() if publico == "assinantes" else db.todos_clientes())
        destinos = [u for u in destinos if u not in cfg.admin_ids]
        await cb.message.answer(f"⏳ Enviando a mensagem paga <code>{pack_id}</code> para {len(destinos)} pessoa(s)…")
        teclado = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🔓 Desbloquear · {pack.preco_fmt}", callback_data=f"comprar:{pack_id}")
        ]])
        legenda = (f"💌 <b>{html.escape(pack.titulo)}</b>\n{_resumo_midias(pack.originais)}\n\n"
                   f"Mensagem exclusiva. Desbloqueie por <b>{pack.preco_fmt}</b> 👇")

        async def tarefa():
            from envio import enviar_em_massa, resumo_envio
            res = await enviar_em_massa(db, cfg, destinos, lambda uid: bot.send_photo(
                uid, pack.blur[0]["file_id"], caption=legenda, reply_markup=teclado))
            await bot.send_message(cb.message.chat.id, "✅ Mensagem paga enviada.\n" + resumo_envio(res))
        asyncio.create_task(tarefa())

    @router.message(StateFilter(NovoPack.titulo, NovoPack.preco))
    async def esperando_texto(msg: Message):
        await msg.answer("Envie uma mensagem de texto, ou /cancelar.")

    return router
