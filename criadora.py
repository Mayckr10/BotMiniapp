"""
Painel da criadora dentro do Mini App: o controle do dia a dia sai do bot e vai para a loja.

Quem está em ADMIN_IDS e abre a loja vê o "modo criadora":
  - editar nome, bio e foto de perfil;
  - publicar textos no feed (com foto opcional, visível para todos);
  - criar packs pagos enviando fotos/vídeos do celular, com título, preço e legenda
    (as amostras com desfoque são geradas aqui e, se quiser, vão para o Canal de Amostras);
  - editar legenda/título/preço e apagar packs e publicações;
  - ver o resumo de vendas.
O bot continua com todos os comandos como plano B (/novo_pack, /perfil, /stats...).

Como as mídias viram file_id do Telegram: o servidor envia cada arquivo para o chat privado da própria
criadora, guarda o file_id e apaga a mensagem na hora. Assim a entrega continua sendo feita pelo bot,
com protect_content, exatamente como nos packs criados pelo /novo_pack.

Rotas (todas exigem initData de um admin):
  GET  /api/criadora/painel
  POST /api/criadora/perfil            multipart: nome, bio, foto
  POST /api/criadora/post              multipart: texto, foto
  POST /api/criadora/post/{id}         json: texto
  POST /api/criadora/post/{id}/apagar
  POST /api/criadora/pack              multipart: titulo, preco, legenda, canal, arquivos (vários)
  POST /api/criadora/pack/{id}         json: titulo, preco, legenda
  POST /api/criadora/pack/{id}/apagar
  GET  /api/loja/post_foto/{id}        (público) foto de uma publicação de texto
"""
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiohttp import web
from PIL import Image, ImageOps

import mensagens
from admin_handlers import parse_preco, postar_no_canal
from blur_service import _capa_video_generica, _frame_ffmpeg, aplicar_blur
from config import Config
from database import Database, agora, fmt_reais

log = logging.getLogger(__name__)
LIMITE_FOTO = 9 * 1024 * 1024          # Telegram aceita fotos de até 10 MB
LIMITE_VIDEO = 49 * 1024 * 1024        # e arquivos de até 50 MB pela Bot API
MAX_ARQUIVOS = 30


def _admin(request: web.Request) -> dict:
    from loja import _usuario            # evita import circular
    user = _usuario(request)
    if user["id"] not in request.app["cfg"].admin_ids:
        raise web.HTTPForbidden(text='{"erro":"so_criadora"}', content_type="application/json")
    return user


def _ok(**dados) -> web.Response:
    return web.json_response({"ok": True, **dados})


def _erro(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "erro": msg}, status=status)


async def _ler_multipart(request: web.Request) -> tuple[dict, list[tuple[str, str, bytes]]]:
    """Devolve (campos de texto, [(nome_do_campo, content_type, bytes)])."""
    campos, arquivos = {}, []
    if not request.content_type.startswith("multipart/"):          # formulário simples, sem arquivos
        return {k: str(v).strip() for k, v in (await request.post()).items()}, []
    leitor = await request.multipart()
    async for parte in leitor:
        if parte.filename:
            dados = await parte.read(decode=False)
            if dados:
                arquivos.append((parte.name, parte.headers.get("Content-Type", ""), bytes(dados)))
        else:
            campos[parte.name] = (await parte.text()).strip()
    return campos, arquivos


def _ajustar_foto(dados: bytes) -> bytes:
    """Converte HEIC/PNG/etc. em JPEG e reduz se passar do limite do Telegram."""
    img = ImageOps.exif_transpose(Image.open(BytesIO(dados)))     # foto do celular: aplica a rotação do EXIF
    img = img.convert("RGB")
    lado = 4096
    while True:
        img.thumbnail((lado, lado))
        saida = BytesIO()
        img.save(saida, "JPEG", quality=90)
        if saida.tell() <= LIMITE_FOTO or lado <= 1280:
            return saida.getvalue()
        lado = int(lado * 0.75)


async def _subir(bot: Bot, chat_id: int, tipo: str, dados: bytes, nome: str) -> str:
    """Envia para o chat da criadora, pega o file_id e apaga a mensagem (fica só o arquivo no Telegram)."""
    arquivo = BufferedInputFile(dados, filename=nome)
    if tipo == "photo":
        msg = await bot.send_photo(chat_id, arquivo, disable_notification=True)
        file_id = msg.photo[-1].file_id
    else:
        msg = await bot.send_video(chat_id, arquivo, disable_notification=True, supports_streaming=True)
        file_id = msg.video.file_id
    try:
        await bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass
    return file_id


def _tipo(content_type: str, nome_campo: str, dados: bytes) -> str | None:
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("image/"):
        return "photo"
    try:                                     # sem content-type confiável: tenta abrir como imagem
        Image.open(BytesIO(dados)).verify()
        return "photo"
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
async def painel(request: web.Request) -> web.Response:
    _admin(request)
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    fuso = ZoneInfo(cfg.fuso_horario)
    hoje = datetime.now(fuso).replace(hour=0, minute=0, second=0, microsecond=0)
    periodos = {"hoje": hoje, "7d": agora() - timedelta(days=7), "30d": agora() - timedelta(days=30)}
    st = {k: await db.stats_periodo(v) for k, v in periodos.items()}
    s_hoje, s_7d, s_30d = st["hoje"], st["7d"], st["30d"]
    vip = await db.resumo_assinaturas()
    packs = await db.listar_packs()
    curtidas = await db.curtidas_do_feed(0)
    mimos = await db.mimos_do_feed()

    # gráfico dos últimos 14 dias (no fuso da criadora)
    offset = int(hoje.utcoffset().total_seconds() // 3600)
    por_dia = await db.vendas_por_dia(hoje - timedelta(days=13), offset)
    grafico = [{"dia": (hoje - timedelta(days=13 - i)).strftime("%d/%m"),
                "v": por_dia.get((hoje - timedelta(days=13 - i)).strftime("%Y-%m-%d"), 0)} for i in range(14)]
    top = []
    for t in await db.top_itens(periodos["30d"]):
        item = await db.get_item(t["pack_id"])
        top.append({"id": t["pack_id"], "titulo": item.titulo if item else t["pack_id"], "vendas": t["n"],
                    "total_fmt": fmt_reais(t["total"]), "curtidas": curtidas.get(t["pack_id"], {}).get("n", 0)})
    tipos = await db.receita_por_tipo(periodos["30d"])
    stats = {k: {"faturamento": fmt_reais(v["faturamento"]), "vendas": v["vendas"],
                 "ticket": fmt_reais(v["ticket_medio"]), "cliques": v["cliques"], "compras": v["compras_unicas"],
                 "conversao": round(v["conversao"] * 100) if v["conversao"] is not None else None,
                 "novos": v["novos_usuarios"], "cupom": v["com_cupom"], "pix": v["pix_gerados"],
                 "curtidas": await db.curtidas_desde(periodos[k])} for k, v in st.items()}
    return web.json_response({
        "resumo": {
            "hoje": {"vendas": s_hoje["vendas"], "valor": fmt_reais(s_hoje["faturamento"])},
            "semana": {"vendas": s_7d["vendas"], "valor": fmt_reais(s_7d["faturamento"])},
            "mes": {"vendas": s_30d["vendas"], "valor": fmt_reais(s_30d["faturamento"])},
            "assinantes": vip["ativos"], "vencendo": vip["vencendo_7d"],
            "packs": len(packs),
        },
        "stats": {
            "periodos": stats,
            "grafico": grafico,
            "top": top,
            "receita_tipos": {k: fmt_reais(tipos.get(k, 0)) for k in ("packs", "vip", "mimos")},
            "receita_tipos_v": {k: tipos.get(k, 0) for k in ("packs", "vip", "mimos")},
            "vip_receita": fmt_reais(vip["receita_30d"]),
            "pendentes": await db.pendentes_abertos(),
            "curtidas_total": sum(c["n"] for c in curtidas.values()),
            "mimos_total": fmt_reais(sum(m["total"] for m in mimos.values())),
        },
        "perfil": {"nome": await db.get_ajuste("nome", cfg.criadora_nome),
                   "bio": await db.get_ajuste("bio", "") or ""},
        "canal": bool(cfg.channel_id),
        "mensagens": [{"chave": k, "rotulo": mensagens.ROTULOS[k], "texto": await db.get_ajuste(f"msg_{k}", "") or "",
                       "padrao": mensagens.PADRAO[k], "foto": bool(await mensagens.foto(db, k))}
                      for k in mensagens.PADRAO],
    })


async def perfil(request: web.Request) -> web.Response:
    user = _admin(request)
    db: Database = request.app["db"]
    campos, arquivos = await _ler_multipart(request)
    if "nome" in campos and campos["nome"]:
        await db.set_ajuste("nome", campos["nome"][:60])
        object.__setattr__(request.app["cfg"], "criadora_nome", campos["nome"][:60])   # vale na hora, sem reiniciar
    if "bio" in campos:
        await db.set_ajuste("bio", campos["bio"][:700])
    fotos = [a for a in arquivos if a[0] == "foto"]
    if fotos:
        try:
            jpg = await asyncio.to_thread(_ajustar_foto, fotos[0][2])
        except Exception:
            return _erro("Não consegui ler essa imagem.")
        await db.set_ajuste("foto_perfil", await _subir(request.app["bot"], user["id"], "photo", jpg, "perfil.jpg"))
    return _ok()


async def novo_post(request: web.Request) -> web.Response:
    user = _admin(request)
    db: Database = request.app["db"]
    campos, arquivos = await _ler_multipart(request)
    texto = campos.get("texto", "")[:4000]
    if not texto and not arquivos:
        return _erro("Escreva algo ou escolha uma foto.")
    foto = None
    if arquivos:
        try:
            jpg = await asyncio.to_thread(_ajustar_foto, arquivos[0][2])
        except Exception:
            return _erro("Não consegui ler essa imagem.")
        foto = await _subir(request.app["bot"], user["id"], "photo", jpg, "post.jpg")
    return _ok(id=await db.criar_post(texto, foto))


async def editar_post(request: web.Request) -> web.Response:
    _admin(request)
    db: Database = request.app["db"]
    post_id = int(request.match_info["id"])
    if not await db.get_post(post_id):
        return _erro("Publicação não encontrada.", 404)
    await db.editar_post(post_id, str((await request.json()).get("texto", ""))[:4000])
    return _ok()


async def apagar_post(request: web.Request) -> web.Response:
    _admin(request)
    await request.app["db"].apagar_post(int(request.match_info["id"]))
    return _ok()


async def novo_pack(request: web.Request) -> web.Response:
    user = _admin(request)
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    campos, arquivos = await _ler_multipart(request)
    titulo = campos.get("titulo", "")[:80]
    preco = parse_preco(campos.get("preco", ""))
    if not titulo:
        return _erro("Dê um título ao pack.")
    if not preco:
        return _erro("Preço inválido. Use algo como 29,90 (mínimo R$ 1,00).")
    if not arquivos:
        return _erro("Escolha pelo menos uma foto ou vídeo.")
    if len(arquivos) > MAX_ARQUIVOS:
        return _erro(f"Máximo de {MAX_ARQUIVOS} arquivos por pack.")

    originais, blur = [], []
    for i, (_, ctype, dados) in enumerate(arquivos):
        tipo = _tipo(ctype, "", dados)
        if tipo is None:
            return _erro(f"O arquivo {i + 1} não é foto nem vídeo.")
        if tipo == "video" and len(dados) > LIMITE_VIDEO:
            return _erro(f"O vídeo {i + 1} passa de 50 MB, o limite do Telegram para bots. "
                         "Envie esse pelo bot (/novo_pack) ou comprima antes.")
        try:
            if tipo == "photo":
                dados = await asyncio.to_thread(_ajustar_foto, dados)
                teaser = await asyncio.to_thread(aplicar_blur, dados, cfg.blur_radius)
            else:
                frame = await asyncio.to_thread(_frame_ffmpeg, dados)
                teaser = await asyncio.to_thread(aplicar_blur, frame or _capa_video_generica(),
                                                 cfg.blur_radius if frame else 0, "VIDEO BLOQUEADO")
            originais.append({"tipo": tipo, "file_id": await _subir(bot, user["id"], tipo, dados,
                                                                     f"m{i}.{'jpg' if tipo == 'photo' else 'mp4'}")})
            blur.append({"tipo": "photo", "file_id": await _subir(bot, user["id"], "photo",
                                                                  teaser.getvalue(), f"b{i}.jpg")})
        except Exception:
            log.exception("Painel: falha ao processar o arquivo %s", i + 1)
            return _erro(f"Não consegui processar o arquivo {i + 1}. Tente de novo.", 502)

    pack_id = await db.criar_pack(titulo, preco, originais, blur)
    await db.editar_pack(pack_id, titulo, preco, campos.get("legenda", "")[:2000])
    no_canal = False
    if campos.get("canal") == "1" and cfg.channel_id:
        try:
            await postar_no_canal(bot, cfg, await db.get_pack(pack_id))
            no_canal = True
        except Exception:
            log.exception("Painel: não consegui postar no canal")
    return _ok(id=pack_id, no_canal=no_canal)


async def editar_pack(request: web.Request) -> web.Response:
    _admin(request)
    db: Database = request.app["db"]
    pack = await db.get_pack(request.match_info["id"])
    if not pack:
        return _erro("Pack não encontrado.", 404)
    corpo = await request.json()
    preco = parse_preco(str(corpo.get("preco", ""))) if corpo.get("preco") else pack.preco
    if not preco:
        return _erro("Preço inválido. Use algo como 29,90.")
    await db.editar_pack(pack.id, (corpo.get("titulo") or pack.titulo)[:80], preco,
                         str(corpo.get("legenda", pack.legenda))[:2000])
    return _ok()


async def apagar_pack(request: web.Request) -> web.Response:
    _admin(request)
    await request.app["db"].apagar_pack(request.match_info["id"])
    return _ok()


async def salvar_mensagem(request: web.Request) -> web.Response:
    user = _admin(request)
    db: Database = request.app["db"]
    campos, arquivos = await _ler_multipart(request)
    chave = campos.get("chave", "")
    if chave not in mensagens.PADRAO:
        return _erro("Mensagem desconhecida.")
    await db.set_ajuste(f"msg_{chave}", campos.get("texto", "")[:3500])
    if campos.get("remover_foto") == "1":
        await db.set_ajuste(f"msg_{chave}_foto", None)
    if arquivos:
        try:
            jpg = await asyncio.to_thread(_ajustar_foto, arquivos[0][2])
        except Exception:
            return _erro("Não consegui ler essa imagem.")
        await db.set_ajuste(f"msg_{chave}_foto", await _subir(request.app["bot"], user["id"], "photo", jpg, "msg.jpg"))
    return _ok()


async def mensagem_foto(request: web.Request) -> web.Response:
    from loja import _imagem
    file_id = await mensagens.foto(request.app["db"], request.match_info["chave"])
    if not file_id:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], file_id), content_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})


async def post_foto(request: web.Request) -> web.Response:
    from loja import _imagem
    post = await request.app["db"].get_post(int(request.match_info["id"]))
    if not post or not post["foto"]:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], post["foto"]), content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/api/criadora/painel", painel)
    app.router.add_post("/api/criadora/perfil", perfil)
    app.router.add_post("/api/criadora/post", novo_post)
    app.router.add_post("/api/criadora/post/{id:\\d+}", editar_post)
    app.router.add_post("/api/criadora/post/{id:\\d+}/apagar", apagar_post)
    app.router.add_post("/api/criadora/pack", novo_pack)
    app.router.add_post("/api/criadora/pack/{id}/apagar", apagar_pack)
    app.router.add_post("/api/criadora/pack/{id}", editar_pack)
    app.router.add_get("/api/loja/post_foto/{id:\\d+}", post_foto)
    app.router.add_post("/api/criadora/mensagem", salvar_mensagem)
    app.router.add_get("/api/loja/msg_foto/{chave}", mensagem_foto)
