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
import json
import logging
import tempfile
from pathlib import Path
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiohttp import web
from PIL import Image, ImageOps

import mensagens
import protecao
from admin_handlers import parse_preco, postar_no_canal
from blur_service import _capa_video_generica, _frame_ffmpeg, aplicar_blur, comprimir_video
from config import Config
from assinaturas import data_local
from database import Database, agora, fmt_reais

log = logging.getLogger(__name__)
LIMITE_FOTO = 9 * 1024 * 1024          # Telegram aceita fotos de até 10 MB
LIMITE_VIDEO_POST = 19 * 1024 * 1024   # vídeo de publicação grátis: a loja toca pelo download da Bot API (20 MB)
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


async def _ler_multipart(request: web.Request, em_disco: bool = False) -> tuple[dict, list[tuple[str, str, bytes]]]:
    """Devolve (campos de texto, [(nome_do_campo, content_type, bytes)]).
    Com em_disco=True, arquivos grandes (vídeos) vão para um arquivo temporário e voltam como Path,
    para não segurar centenas de MB na memória."""
    campos, arquivos = {}, []
    if not request.content_type.startswith("multipart/"):          # formulário simples, sem arquivos
        return {k: str(v).strip() for k, v in (await request.post()).items()}, []
    leitor = await request.multipart()
    async for parte in leitor:
        if parte.filename:
            if em_disco:
                tmp = Path(tempfile.mkstemp(prefix="up_", suffix=".bin")[1])
                with tmp.open("wb") as f:
                    while pedaco := await parte.read_chunk(1024 * 1024):
                        f.write(pedaco)
                tam = tmp.stat().st_size
                if not tam:
                    tmp.unlink(missing_ok=True)
                    continue
                dados = tmp if tam > LIMITE_VIDEO else tmp.read_bytes()
                if not isinstance(dados, Path):
                    tmp.unlink(missing_ok=True)
                arquivos.append((parte.name, parte.headers.get("Content-Type", ""), dados))
                continue
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


def _quando(campos: dict) -> tuple[datetime | None, str | None]:
    """Lê o campo 'agendar' (ISO enviado pelo app). Devolve (data, erro)."""
    bruto = (campos.get("agendar") or "").strip()
    if not bruto:
        return None, None
    try:
        quando = datetime.fromisoformat(bruto.replace("Z", "+00:00"))
    except ValueError:
        return None, "Data do agendamento inválida."
    if quando.tzinfo is None:
        return None, "Data do agendamento sem fuso."
    if quando < agora() + timedelta(minutes=1):
        return None, "Escolha um horário no futuro."
    if quando > agora() + timedelta(days=90):
        return None, "Agende para no máximo 90 dias."
    return quando, None


def _enquete(campos: dict) -> tuple[list[str] | None, str | None]:
    bruto = campos.get("enquete") or ""
    if not bruto:
        return None, None
    try:
        opcoes = [str(o).strip()[:60] for o in json.loads(bruto) if str(o).strip()]
    except (ValueError, TypeError):
        return None, "Enquete inválida."
    if not 2 <= len(opcoes) <= 4:
        return None, "A enquete precisa de 2 a 4 opções."
    return opcoes, None


async def novo_post(request: web.Request) -> web.Response:
    user = _admin(request)
    db: Database = request.app["db"]
    campos, arquivos = await _ler_multipart(request, em_disco=True)
    try:
        return await _novo_post(request, user, db, campos, arquivos)
    finally:
        for _, _, d in arquivos:
            if isinstance(d, Path):
                d.unlink(missing_ok=True)


async def _video_do_post(bot: Bot, chat_id: int, dados) -> tuple[str, str] | str:
    """Prepara o vídeo de uma publicação grátis. Devolve (file_id da capa, file_id do vídeo) ou a mensagem de erro.
    Fica com até 19 MB: a loja só consegue buscar no Telegram arquivos de até 20 MB para tocar no feed."""
    if isinstance(dados, Path) or len(dados) > LIMITE_VIDEO_POST:
        origem, tmp = dados, None
        if not isinstance(dados, Path):
            tmp = Path(tempfile.mkstemp(prefix="post_", suffix=".mp4")[1])
            tmp.write_bytes(dados)
            origem = tmp
        try:
            dados = await asyncio.to_thread(comprimir_video, origem, LIMITE_VIDEO_POST)
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)
        if dados is None:
            return ("Não consegui preparar esse vídeo para o feed. Ele pode ser longo demais: "
                    "corte em partes de até uns 3 minutos.")
    frame = await asyncio.to_thread(_frame_ffmpeg, dados)
    capa = await asyncio.to_thread(_ajustar_foto, frame) if frame else _capa_video_generica()
    return (await _subir(bot, chat_id, "photo", capa, "capa.jpg"),
            await _subir(bot, chat_id, "video", dados, "post.mp4"))


async def _novo_post(request: web.Request, user: dict, db: Database, campos: dict, arquivos: list) -> web.Response:
    texto = campos.get("texto", "")[:4000]
    enquete, erro = _enquete(campos)
    quando, erro2 = _quando(campos)
    if erro or erro2:
        return _erro(erro or erro2)
    if not texto and not arquivos:
        return _erro("Escreva algo ou escolha uma foto.")
    foto = video = None
    if arquivos:
        _, ctype, dados = arquivos[0]
        eh_video = isinstance(dados, Path) or _tipo(ctype, "", dados) == "video"
        if eh_video:
            try:
                r = await _video_do_post(request.app["bot"], user["id"], dados)
            except Exception:
                log.exception("Painel: falha no vídeo da publicação")
                return _erro("Não consegui processar o vídeo. Tente de novo.", 502)
            if isinstance(r, str):
                return _erro(r)
            foto, video = r
        else:
            try:
                jpg = await asyncio.to_thread(_ajustar_foto, dados)
            except Exception:
                return _erro("Não consegui ler essa imagem.")
            foto = await _subir(request.app["bot"], user["id"], "photo", jpg, "post.jpg")
    post_id = await db.criar_post(texto, foto, enquete, video)
    if quando:                                 # agendado: ela já decidiu quando vai ao ar, não passa pela fila
        await db.agendar_post(post_id, quando)
        return _ok(id=post_id, agendado=data_local(quando, request.app["cfg"]))
    if await _revisao(db):
        await db.mandar_post_para_fila(post_id)
        return _ok(id=post_id, fila=True)
    return _ok(id=post_id)


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
    campos, arquivos = await _ler_multipart(request, em_disco=True)
    try:
        return await _novo_pack(request, user, db, cfg, bot, campos, arquivos)
    finally:
        for _, _, d in arquivos:
            if isinstance(d, Path):
                d.unlink(missing_ok=True)


async def _novo_pack(request, user, db: Database, cfg: Config, bot: Bot, campos: dict, arquivos: list) -> web.Response:
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
    quando, erro = _quando(campos)
    if erro:
        return _erro(erro)

    originais, blur = [], []
    for i, (_, ctype, dados) in enumerate(arquivos):
        grande = isinstance(dados, Path)
        tipo = "video" if grande and ctype.startswith("video") else _tipo(ctype, "", dados if not grande else b"")
        if tipo is None:
            return _erro(f"O arquivo {i + 1} não é foto nem vídeo.")
        if tipo == "video" and grande:            # passa de 50 MB: comprime para caber no limite do Telegram
            dados = await asyncio.to_thread(comprimir_video, dados, LIMITE_VIDEO)
            if dados is None:
                return _erro(f"Não consegui deixar o vídeo {i + 1} abaixo de 50 MB (o limite do Telegram). "
                             "Ele pode ser longo demais: corte em partes ou envie pelo bot.")
        elif grande:
            return _erro(f"O arquivo {i + 1} é grande demais.")
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
    if quando:
        await db.agendar_pack(pack_id, quando, campos.get("contagem") == "1", campos.get("canal") == "1")
        return _ok(id=pack_id, agendado=data_local(quando, cfg))
    if await _revisao(db):                    # fica na fila: nada aparece nem vai para o canal até aprovar
        await db.mandar_pack_para_fila(pack_id, campos.get("canal") == "1")
        return _ok(id=pack_id, fila=True)
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


async def post_video(request: web.Request) -> web.Response:
    from galeria import _baixar, _resposta_com_range
    post = await request.app["db"].get_post(int(request.match_info["id"]))
    if not post or not post.get("video"):
        raise web.HTTPNotFound()
    try:
        dados = await _baixar(request.app["bot"], post["video"])
    except Exception:
        log.exception("Não consegui baixar o vídeo do post %s", post["id"])
        raise web.HTTPNotFound()
    resp = _resposta_com_range(request, dados, "video/mp4")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


async def _revisao(db: Database) -> bool:
    return await db.get_ajuste("revisao", "0") == "1"


async def ajustes(request: web.Request) -> web.Response:
    _admin(request)
    corpo = await request.json()
    db: Database = request.app["db"]
    if "revisao" in corpo:
        await db.set_ajuste("revisao", "1" if corpo["revisao"] else "0")
    if "canal_padrao" in corpo:
        await db.set_ajuste("canal_padrao", "1" if corpo["canal_padrao"] else "0")
    for chave in ("notif_vendas", "resumo_diario", "marca_dagua"):
        if chave in corpo:
            await db.set_ajuste(chave, "1" if corpo[chave] else "0")
    if "limite_reenvio" in corpo:
        try:
            limite = int(corpo["limite_reenvio"])
        except (TypeError, ValueError):
            return _erro("Limite inválido.")
        if not 0 <= limite <= 50:
            return _erro("Use de 0 (sem limite) a 50 reenvios.")
        await db.set_ajuste("limite_reenvio", str(limite))
    if "vip_1mes_pct" in corpo:
        try:
            pct = int(str(corpo["vip_1mes_pct"]).replace("%", "").strip() or 0)
        except ValueError:
            return _erro("Desconto inválido.")
        if not 0 <= pct <= 90:
            return _erro("O desconto do 1º mês deve ser de 1 a 90% (0 desliga).")
        await db.set_ajuste("vip_1mes_pct", str(pct))
    if "gorjetas" in corpo:
        valores = sorted({int(v) for v in str(corpo["gorjetas"]).replace(";", ",").split(",")
                          if v.strip().isdigit() and 1 <= int(v) <= 10000})
        if not valores:
            return _erro("Informe pelo menos um valor, ex: 10, 25, 50, 100")
        await db.set_ajuste("gorjetas", ",".join(map(str, valores[:8])))
    return _ok()


async def config(request: web.Request) -> web.Response:
    """Tudo que a aba Ajustes mostra: planos, cupons, mimos e preferências."""
    _admin(request)
    db: Database = request.app["db"]
    cupons = []
    for c in await db.listar_cupons():
        cupons.append({"codigo": c.codigo, "descricao": c.descricao, "usos": c.usos, "usos_max": c.usos_max,
                       "expira": data_local(c.expira_em, request.app["cfg"]) if c.expira_em else None})
    return web.json_response({
        "planos": [{"id": p.id, "nome": p.nome, "dias": p.dias, "preco": p.preco, "preco_fmt": p.preco_fmt}
                   for p in await db.listar_planos()],
        "cupons": cupons,
        "gorjetas": await mensagens.valores_gorjeta(),
        "revisao": await db.get_ajuste("revisao", "0") == "1",
        "canal_padrao": await db.get_ajuste("canal_padrao", "1") == "1",
        "canal": bool(request.app["cfg"].channel_id),
        "notif_vendas": await db.get_ajuste("notif_vendas", "1") == "1",
        "resumo_diario": await db.get_ajuste("resumo_diario", "1") == "1",
        "resumo_hora": request.app["cfg"].resumo_hora,
        "limite_reenvio": await protecao.limite_reenvio(db),
        "marca_dagua": await db.get_ajuste("marca_dagua", "1") == "1",
        "vip_1mes_pct": int(await db.get_ajuste("vip_1mes_pct", "0") or 0),
        "bloqueados": [{"id": u["user_id"], "nome": u["nome"] or "", "username": u["username"] or "",
                        "desde": data_local(u["banido_em"], request.app["cfg"]) if u["banido_em"] else ""}
                       for u in await db.listar_banidos()],
    })


async def bloquear(request: web.Request) -> web.Response:
    """Bloqueia (ou desbloqueia) um fã pelo ID do Telegram."""
    _admin(request)
    corpo = await request.json()
    try:
        user_id = int(str(corpo.get("user_id", "")).strip())
    except ValueError:
        return _erro("ID inválido. É um número, como 123456789.")
    if user_id <= 0:
        return _erro("ID inválido. É um número, como 123456789.")
    db: Database = request.app["db"]
    if corpo.get("bloquear", True):
        erro = await protecao.bloquear(request.app["bot"], db, request.app["cfg"], user_id)
        if erro:
            return _erro(erro)
    else:
        await protecao.desbloquear(db, user_id)
    return _ok()


async def fa(request: web.Request) -> web.Response:
    """Ficha de um fã para a criadora: quem é, quanto já gastou, VIP e se está bloqueado."""
    _admin(request)
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    user_id = int(request.match_info["id"])
    u = await db.get_usuario(user_id) or {}
    gasto = await db.total_gasto(user_id)
    a = await db.get_assinatura(user_id)
    return _ok(id=user_id, nome=u.get("nome") or "", username=u.get("username") or "",
               desde=data_local(u["criado_em"], cfg) if u.get("criado_em") else "",
               compras=gasto["compras"], total=fmt_reais(gasto["total"]), bloqueado=bool(u.get("banido")),
               vip=data_local(a.expira_em, cfg) if a and a.ativa else None)


async def salvar_plano(request: web.Request) -> web.Response:
    _admin(request)
    db: Database = request.app["db"]
    corpo = await request.json()
    nome = str(corpo.get("nome", "")).strip()[:40]
    try:
        dias = int(corpo.get("dias", 0))
    except (TypeError, ValueError):
        dias = 0
    preco = parse_preco(str(corpo.get("preco", "")))
    if not nome:
        return _erro("Dê um nome ao plano (ex: Mensal).")
    if not 1 <= dias <= 3650:
        return _erro("Duração inválida: use de 1 a 3650 dias.")
    if not preco:
        return _erro("Preço inválido. Use algo como 29,90.")
    if corpo.get("id"):
        if not await db.atualizar_plano(int(corpo["id"]), nome, dias, preco):
            return _erro("Plano não encontrado.", 404)
        return _ok(id=int(corpo["id"]))
    return _ok(id=await db.salvar_plano(nome, dias, preco))


async def apagar_plano(request: web.Request) -> web.Response:
    _admin(request)
    db: Database = request.app["db"]
    if len(await db.listar_planos()) <= 1:
        return _erro("Mantenha pelo menos um plano ativo.")
    await db.desativar_plano(int(request.match_info["id"]))
    return _ok()


async def salvar_cupom(request: web.Request) -> web.Response:
    _admin(request)
    db: Database = request.app["db"]
    corpo = await request.json()
    sem_acento = unicodedata.normalize("NFKD", str(corpo.get("codigo", ""))).encode("ascii", "ignore").decode()
    codigo = "".join(ch for ch in sem_acento.upper() if ch.isalnum())[:20]
    tipo = "fixo" if corpo.get("tipo") == "fixo" else "pct"
    if len(codigo) < 3:
        return _erro("O código precisa de pelo menos 3 letras ou números.")
    if tipo == "pct":
        try:
            valor = int(str(corpo.get("valor", "")).replace("%", "").strip())
        except ValueError:
            valor = 0
        if not 1 <= valor <= 90:
            return _erro("Desconto em % deve ser de 1 a 90.")
    else:
        valor = parse_preco(str(corpo.get("valor", "")))
        if not valor:
            return _erro("Valor do desconto inválido. Use algo como 5,00.")
    usos = str(corpo.get("usos_max", "")).strip()
    dias = str(corpo.get("dias", "")).strip()
    await db.criar_cupom(codigo, tipo, valor, usos_max=int(usos) if usos.isdigit() and int(usos) > 0 else None,
                         expira_em=agora() + timedelta(days=int(dias)) if dias.isdigit() and int(dias) > 0 else None)
    return _ok(codigo=codigo)


async def apagar_cupom(request: web.Request) -> web.Response:
    _admin(request)
    await request.app["db"].desativar_cupom(request.match_info["codigo"])
    return _ok()


async def aprovar(request: web.Request) -> web.Response:
    """Fila de revisão: publica (ou descarta) um post/pack que estava aguardando."""
    _admin(request)
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    tipo, item_id, acao = request.match_info["tipo"], request.match_info["id"], request.match_info["acao"]
    if tipo == "post":
        if not item_id.isdigit():
            return _erro("Publicação não encontrada.", 404)
        if acao == "descartar":
            await db.apagar_post(int(item_id))
            return _ok()
        return _ok() if await db.aprovar_post(int(item_id)) else _erro("Essa publicação não está na fila.", 404)
    if acao == "descartar":
        await db.descartar_pack(item_id)
        return _ok()
    pack = await db.get_pack(item_id)
    if not pack or pack.tipo != "fila":
        return _erro("Esse pack não está na fila.", 404)
    canal = await db.aprovar_pack(item_id)
    no_canal = False
    if canal and cfg.channel_id:
        try:
            await postar_no_canal(request.app["bot"], cfg, await db.get_pack(item_id))
            no_canal = True
        except Exception:
            log.exception("Fila: não consegui postar no canal")
    return _ok(no_canal=no_canal)


async def agendado(request: web.Request) -> web.Response:
    """Publicar agora, mudar o horário ou cancelar um post/pack agendado."""
    _admin(request)
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    tipo, item_id, acao = request.match_info["tipo"], request.match_info["id"], request.match_info["acao"]
    if tipo == "post" and not item_id.isdigit():
        return _erro("Publicação não encontrada.", 404)
    ref = int(item_id) if tipo == "post" else item_id
    if acao == "horario":
        quando, erro = _quando(await request.json())
        if erro or not quando:
            return _erro(erro or "Escolha o novo horário.")
        return _ok(agendado=data_local(quando, cfg)) if await db.reagendar(tipo, ref, quando) \
            else _erro("Esse item não está mais agendado.", 404)
    if acao == "cancelar":
        if tipo == "post":
            await db.apagar_post(ref)
        else:
            await db.cancelar_pack_agendado(ref)
        return _ok()
    if tipo == "post":
        return _ok() if await db.publicar_post_agendado(ref) else _erro("Esse post não está mais agendado.", 404)
    from agenda import publicar_pack
    publicou, no_canal = await publicar_pack(request.app["bot"], db, cfg, ref)
    return _ok(no_canal=no_canal) if publicou else _erro("Esse pack não está mais agendado.", 404)


def registrar_rotas(app: web.Application) -> None:
    app.router.add_post("/api/criadora/agendado/{tipo:post|pack}/{id}/{acao:publicar|cancelar|horario}", agendado)
    app.router.add_post("/api/criadora/bloquear", bloquear)
    app.router.add_get("/api/criadora/fa/{id:\\d+}", fa)
    app.router.add_post("/api/criadora/ajustes", ajustes)
    app.router.add_get("/api/criadora/config", config)
    app.router.add_post("/api/criadora/plano", salvar_plano)
    app.router.add_post("/api/criadora/plano/{id:\\d+}/apagar", apagar_plano)
    app.router.add_post("/api/criadora/cupom", salvar_cupom)
    app.router.add_post("/api/criadora/cupom/{codigo}/apagar", apagar_cupom)
    app.router.add_post("/api/criadora/fila/{tipo:post|pack}/{id}/{acao:aprovar|descartar}", aprovar)
    app.router.add_get("/api/criadora/painel", painel)
    app.router.add_post("/api/criadora/perfil", perfil)
    app.router.add_post("/api/criadora/post", novo_post)
    app.router.add_post("/api/criadora/post/{id:\\d+}", editar_post)
    app.router.add_post("/api/criadora/post/{id:\\d+}/apagar", apagar_post)
    app.router.add_post("/api/criadora/pack", novo_pack)
    app.router.add_post("/api/criadora/pack/{id}/apagar", apagar_pack)
    app.router.add_post("/api/criadora/pack/{id}", editar_pack)
    app.router.add_get("/api/loja/post_foto/{id:\\d+}", post_foto)
    app.router.add_get("/api/loja/post_video/{id:\\d+}", post_video)
    app.router.add_post("/api/criadora/mensagem", salvar_mensagem)
    app.router.add_get("/api/loja/msg_foto/{chave}", mensagem_foto)
