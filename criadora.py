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
  POST /api/criadora/story                    multipart: arquivo, legenda (some em 24h)
  POST /api/criadora/story/{id}/apagar        · GET /api/criadora/story/{id}/vistos (quem viu)
  POST /api/criadora/pedido/{id}/entregar     multipart: arquivos, mensagem (vira um pack só do fã)
  POST /api/criadora/pedido/{id}/recusar      json: mensagem
  POST /api/criadora/meta                     json: titulo, descricao, alvo · POST /api/criadora/meta/encerrar
  GET  /api/loja/post_foto/{id}?u&e&s  foto de uma publicação (link assinado; só VIP e a criadora)
"""
import asyncio
import time
import html
import json
import logging
import tempfile
from pathlib import Path
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
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


def _admin(request: web.Request, perm: str | None = None) -> dict:
    """A criadora (ou alguém da equipe com a permissão `perm`). Sem perm: qualquer pessoa da equipe."""
    from loja import _usuario            # evita import circular
    user = _usuario(request)
    perms = request.app["contas"].permissoes(user["id"])
    if perms is None:
        raise web.HTTPForbidden(text='{"erro":"so_criadora"}', content_type="application/json")
    if perm and perm not in perms:
        raise web.HTTPForbidden(text='{"erro":"Você não tem permissão para isso. Fale com a criadora."}',
                                content_type="application/json")
    user["permissoes"] = perms
    return user


def _ok(**dados) -> web.Response:
    return web.json_response({"ok": True, **dados})


def _erro(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "erro": msg}, status=status)


async def _ler_multipart(request: web.Request, em_disco: bool = False) -> tuple[dict, list[tuple[str, str, bytes]]]:
    """Devolve (campos de texto, [(nome_do_campo, content_type, bytes)]).
    Com em_disco=True, cada arquivo vai para um arquivo temporário e volta como Path: o servidor tem 1 GB de RAM
    e um pack de 20 vídeos passa disso fácil. Os arquivos são lidos um por vez na hora de processar."""
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
                arquivos.append((parte.name, parte.headers.get("Content-Type", ""), tmp))
                continue
            dados = await parte.read(decode=False)
            if dados:
                arquivos.append((parte.name, parte.headers.get("Content-Type", ""), bytes(dados)))
        else:
            campos[parte.name] = (await parte.text()).strip()
    return campos, arquivos


_TAREFAS: set[asyncio.Task] = set()
_FILA: asyncio.Semaphore | None = None          # uma publicação pesada por vez (memória e CPU limitadas)


def _fila() -> asyncio.Semaphore:
    global _FILA
    if _FILA is None:
        _FILA = asyncio.Semaphore(1)
    return _FILA


def _tamanho(d) -> int:
    return d.stat().st_size if isinstance(d, Path) else len(d)


def _tipo_arquivo(ctype: str, d) -> str | None:
    if ctype.startswith("video/"):
        return "video"
    if ctype.startswith("image/"):
        return "photo"
    try:                                     # sem content-type confiável: tenta abrir como imagem
        Image.open(d if isinstance(d, Path) else BytesIO(d)).verify()
        return "photo"
    except Exception:
        return None


def _limpar(arquivos: list) -> None:
    for _, _, d in arquivos:
        if isinstance(d, Path):
            d.unlink(missing_ok=True)


def _em_segundo_plano(request: web.Request, user: dict, arquivos: list, rotulo: str, trabalho) -> web.Response:
    """Envio pesado (vídeos ou muitos arquivos): responde na hora e processa em segundo plano, um envio por vez.
    A criadora acompanha o andamento numa mensagem do bot que vai sendo atualizada."""
    bot: Bot = request.app["bot"]

    async def rodar():
        aviso = None
        try:
            aviso = await bot.send_message(user["id"], f"⏳ Preparando: {rotulo}…", disable_notification=True)
        except Exception:
            pass
        ultimo = [0.0]

        async def progresso(feitos: int, total: int):
            if not aviso or (time.monotonic() - ultimo[0] < 3 and feitos < total):
                return
            ultimo[0] = time.monotonic()
            barra = "▰" * round(10 * feitos / total) + "▱" * (10 - round(10 * feitos / total))
            try:
                await aviso.edit_text(f"⏳ Preparando: {rotulo}\n{barra} {feitos}/{total} arquivo(s)")
            except Exception:
                pass

        try:
            async with _fila():
                resp = await trabalho(progresso)
            r = json.loads(resp.body)
            if r.get("ok"):
                onde = (("entregue ao fã ✅" if r.get("no_chat") else "liberado na loja do fã ✅ (não consegui mandar no chat dele)")
                        if r.get("entregue") else
                        f"agendado para {r['agendado']} 🗓️" if r.get("agendado") else
                        "na fila de revisão 📝" if r.get("fila") else
                        "no ar e postado no canal ✅" if r.get("no_canal") else "no ar ✅")
                texto = f"🎬 {rotulo}: {onde}"
            else:
                texto = f"❌ Não consegui publicar ({rotulo}): {r.get('erro', 'erro desconhecido')}"
        except Exception:
            log.exception("Painel: falha ao publicar em segundo plano")
            texto = f"❌ Deu erro ao preparar ({rotulo}). Tente publicar de novo."
        finally:
            _limpar(arquivos)
        try:
            if aviso:
                await aviso.delete()
        except Exception:
            pass
        try:
            await bot.send_message(user["id"], texto)
        except Exception:
            log.warning("Não consegui avisar a criadora sobre a publicação")

    tarefa = asyncio.create_task(rodar())
    _TAREFAS.add(tarefa)
    tarefa.add_done_callback(_TAREFAS.discard)
    return _ok(processando=True)


def _ajustar_foto(dados: bytes) -> bytes:
    """Converte HEIC/PNG/etc. em JPEG e reduz se passar do limite do Telegram."""
    img = Image.open(BytesIO(dados))
    img.draft("RGB", (4096, 4096))            # JPEG gigante: decodifica já reduzido (bem menos memória)
    img = ImageOps.exif_transpose(img)        # foto do celular: aplica a rotação do EXIF
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
    """Envia para o chat da criadora, pega o file_id e apaga a mensagem (fica só o arquivo no Telegram).
    Packs grandes mandam dezenas de arquivos seguidos: se o Telegram pedir para esperar (flood), espera e tenta de novo."""
    for tentativa in range(5):
        arquivo = BufferedInputFile(dados, filename=nome)
        try:
            if tipo == "photo":
                msg = await bot.send_photo(chat_id, arquivo, disable_notification=True)
                file_id = msg.photo[-1].file_id
            else:
                msg = await bot.send_video(chat_id, arquivo, disable_notification=True, supports_streaming=True)
                file_id = msg.video.file_id
            break
        except TelegramRetryAfter as e:
            log.info("Telegram pediu %ss de pausa no envio do painel", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except TelegramNetworkError:
            if tentativa == 4:
                raise
            await asyncio.sleep(3 * (tentativa + 1))
    else:
        raise RuntimeError("Telegram recusou o envio várias vezes")
    try:
        await bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass
    return file_id


# ════════════════════════════════════════════════════════════════════════════
async def painel(request: web.Request) -> web.Response:
    perms = _admin(request)["permissoes"]
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

    async def ranking(desde) -> list[dict]:
        return [{"id": f["user_id"], "nome": f["nome"] or "", "username": f["username"] or "", "compras": f["compras"],
                 "total_fmt": fmt_reais(f["total"]), "vip": await db.assinante_ativo(f["user_id"])}
                for f in await db.top_fas(desde, cfg.admin_ids)]

    pedidos = []
    for pd in await db.listar_pedidos():
        u = await db.get_usuario(pd.user_id) or {}
        pedidos.append({"id": pd.id, "user_id": pd.user_id, "nome": u.get("nome") or "", "username": u.get("username") or "",
                        "texto": pd.texto, "valor_fmt": pd.preco_fmt, "status": pd.status, "pack_id": pd.pack_id,
                        "resposta": pd.resposta or "", "quando": data_local(pd.pago_em or pd.criado_em, cfg)})
    stats = {k: {"faturamento": fmt_reais(v["faturamento"]), "vendas": v["vendas"],
                 "ticket": fmt_reais(v["ticket_medio"]), "cliques": v["cliques"], "compras": v["compras_unicas"],
                 "conversao": round(v["conversao"] * 100) if v["conversao"] is not None else None,
                 "novos": v["novos_usuarios"], "cupom": v["com_cupom"], "pix": v["pix_gerados"],
                 "curtidas": await db.curtidas_desde(periodos[k])} for k, v in st.items()}
    resposta = {
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
        "top_fas": {"30d": await ranking(periodos["30d"]), "sempre": await ranking(None)},
        "pedidos": pedidos,
        "perfil": {"nome": await db.get_ajuste("nome", cfg.criadora_nome),
                   "bio": await db.get_ajuste("bio", "") or ""},
        "canal": bool(cfg.channel_id),
        "mensagens": [{"chave": k, "rotulo": mensagens.ROTULOS[k], "texto": await db.get_ajuste(f"msg_{k}", "") or "",
                       "padrao": mensagens.PADRAO[k], "foto": bool(await mensagens.foto(db, k))}
                      for k in mensagens.PADRAO],
        "permissoes": perms,
    }
    # equipe: só o que a permissão deixa ver
    if "financeiro" not in perms:
        resposta["resumo"] = {"assinantes": vip["ativos"], "vencendo": vip["vencendo_7d"], "packs": len(packs)}
        resposta["stats"] = None
    if "fas" not in perms:
        resposta["top_fas"] = None
    if "pedidos" not in perms:
        resposta["pedidos"] = []
    return web.json_response(resposta)


async def perfil(request: web.Request) -> web.Response:
    user = _admin(request, "ajustes")
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
    user = _admin(request, "publicar")
    db: Database = request.app["db"]
    campos, arquivos = await _ler_multipart(request, em_disco=True)
    pesado = bool(arquivos) and _tipo_arquivo(arquivos[0][1], arquivos[0][2]) == "video"
    if pesado:                                 # vídeo que precisa comprimir: não deixa a criadora esperando
        erro = _enquete(campos)[1] or _quando(campos)[1]
        if erro:
            _limpar(arquivos)
            return _erro(erro)
        return _em_segundo_plano(request, user, arquivos, "Publicação com vídeo",
                                 lambda progresso: _novo_post(request, user, db, campos, arquivos))
    try:
        return await _novo_post(request, user, db, campos, arquivos)
    finally:
        _limpar(arquivos)


async def _video_do_post(bot: Bot, chat_id: int, dados) -> tuple[str, str] | str:
    """Prepara o vídeo de uma publicação grátis. Devolve (file_id da capa, file_id do vídeo) ou a mensagem de erro.
    Fica com até 19 MB: a loja só consegue buscar no Telegram arquivos de até 20 MB para tocar no feed."""
    if isinstance(dados, Path) and dados.stat().st_size <= LIMITE_VIDEO_POST:
        dados = dados.read_bytes()
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
        eh_video = _tipo_arquivo(ctype, dados) == "video"
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
                jpg = await asyncio.to_thread(_ajustar_foto, dados.read_bytes() if isinstance(dados, Path) else dados)
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
    _admin(request, "publicar")
    db: Database = request.app["db"]
    post_id = int(request.match_info["id"])
    if not await db.get_post(post_id):
        return _erro("Publicação não encontrada.", 404)
    await db.editar_post(post_id, str((await request.json()).get("texto", ""))[:4000])
    return _ok()


async def apagar_post(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    await request.app["db"].apagar_post(int(request.match_info["id"]))
    return _ok()


async def novo_pack(request: web.Request) -> web.Response:
    user = _admin(request, "publicar")
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    campos, arquivos = await _ler_multipart(request, em_disco=True)
    pesado = (len(arquivos) > 4 or sum(_tamanho(d) for _, _, d in arquivos) > 40 * 1024 * 1024
              or any(_tipo_arquivo(c, d) == "video" for _, c, d in arquivos))
    if pesado:                                # muitos arquivos ou vídeo: publica em segundo plano
        titulo, preco = campos.get("titulo", "")[:80], parse_preco(campos.get("preco", ""))
        erro = ("Dê um título ao pack." if not titulo else
                "Preço inválido. Use algo como 29,90 (mínimo R$ 1,00)." if not preco else
                f"Máximo de {MAX_ARQUIVOS} arquivos por pack." if len(arquivos) > MAX_ARQUIVOS else
                _quando(campos)[1])
        if erro:
            _limpar(arquivos)
            return _erro(erro)
        return _em_segundo_plano(request, user, arquivos, f"Pack “{html.escape(titulo)}”",
                                 lambda progresso: _novo_pack(request, user, db, cfg, bot, campos, arquivos, progresso))
    try:
        return await _novo_pack(request, user, db, cfg, bot, campos, arquivos)
    finally:
        _limpar(arquivos)


async def _novo_pack(request, user, db: Database, cfg: Config, bot: Bot, campos: dict, arquivos: list,
                     progresso=None) -> web.Response:
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

    r = await _processar_midias(bot, user["id"], cfg, arquivos, progresso)
    if isinstance(r, web.Response):
        return r
    originais, blur = r
    legenda = campos.get("legenda", "")[:2000]
    revisao = not quando and await _revisao(db)
    # agendado ou em revisão nasce "preparando" (fora da vitrine) e muda de estado num UPDATE só
    pack_id = await db.criar_pack(titulo, preco, originais, blur,
                                  tipo="preparando" if quando or revisao else "pack", legenda=legenda)
    if quando:
        await db.agendar_pack(pack_id, quando, campos.get("contagem") == "1", campos.get("canal") == "1")
        return _ok(id=pack_id, agendado=data_local(quando, cfg))
    if revisao:                               # fica na fila: nada aparece nem vai para o canal até aprovar
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


async def _processar_midias(bot: Bot, chat_id: int, cfg: Config, arquivos: list,
                            progresso=None) -> "tuple[list[dict], list[dict]] | web.Response":
    """Sobe cada arquivo para o Telegram (original + amostra embaçada), um por vez na memória."""
    originais, blur = [], []
    for i, (_, ctype, arquivo) in enumerate(arquivos):
        tipo = _tipo_arquivo(ctype, arquivo)
        if tipo is None:
            return _erro(f"O arquivo {i + 1} não é foto nem vídeo.")
        if tipo == "video" and _tamanho(arquivo) > LIMITE_VIDEO:   # passa de 50 MB: comprime para o limite do Telegram
            dados = await asyncio.to_thread(comprimir_video, arquivo, LIMITE_VIDEO)
            if dados is None:
                return _erro(f"Não consegui deixar o vídeo {i + 1} abaixo de 50 MB (o limite do Telegram). "
                             "Ele pode ser longo demais: corte em partes ou envie pelo bot.")
        else:
            dados = arquivo.read_bytes() if isinstance(arquivo, Path) else arquivo
        try:
            if tipo == "photo":
                dados = await asyncio.to_thread(_ajustar_foto, dados)
                teaser = await asyncio.to_thread(aplicar_blur, dados, cfg.blur_radius)
            else:
                frame = await asyncio.to_thread(_frame_ffmpeg, dados)
                teaser = await asyncio.to_thread(aplicar_blur, frame or _capa_video_generica(),
                                                 cfg.blur_radius if frame else 0, "VIDEO BLOQUEADO")
            originais.append({"tipo": tipo, "file_id": await _subir(bot, chat_id, tipo, dados,
                                                                     f"m{i}.{'jpg' if tipo == 'photo' else 'mp4'}")})
            blur.append({"tipo": "photo", "file_id": await _subir(bot, chat_id, "photo",
                                                                  teaser.getvalue(), f"b{i}.jpg")})
            del dados, teaser
            if isinstance(arquivo, Path):
                arquivo.unlink(missing_ok=True)          # libera o disco assim que o arquivo foi para o Telegram
            if progresso:
                await progresso(i + 1, len(arquivos))
        except Exception:
            log.exception("Painel: falha ao processar o arquivo %s", i + 1)
            return _erro(f"Não consegui processar o arquivo {i + 1}. Tente de novo.", 502)
    return originais, blur


async def editar_pack(request: web.Request) -> web.Response:
    _admin(request, "publicar")
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
    _admin(request, "publicar")
    await request.app["db"].apagar_pack(request.match_info["id"])
    return _ok()


async def salvar_mensagem(request: web.Request) -> web.Response:
    user = _admin(request, "ajustes")
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


async def _post_liberado(request: web.Request) -> dict:
    """Post publicado (ou na fila/agendado, para a criadora) + link assinado de quem pode ver (VIP)."""
    from loja import checar_midia
    post = await request.app["db"].get_post(int(request.match_info["id"]))
    if not post:
        raise web.HTTPNotFound()
    await checar_midia(request, f"post_{post['id']}")
    return post


async def post_foto(request: web.Request) -> web.Response:
    from loja import _imagem
    post = await _post_liberado(request)
    if not post["foto"]:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], post["foto"]), content_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


async def post_video(request: web.Request) -> web.Response:
    from galeria import _baixar, _resposta_com_range
    post = await _post_liberado(request)
    if not post.get("video"):
        raise web.HTTPNotFound()
    try:
        dados = await _baixar(request.app["bot"], post["video"])
    except Exception:
        log.exception("Não consegui baixar o vídeo do post %s", post["id"])
        raise web.HTTPNotFound()
    resp = _resposta_com_range(request, dados, "video/mp4")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


# ════════════════════════════════════════════════════════════════════════════
#  Stories (somem em 24h)
# ════════════════════════════════════════════════════════════════════════════
async def novo_story(request: web.Request) -> web.Response:
    user = _admin(request, "publicar")
    db: Database = request.app["db"]
    campos, arquivos = await _ler_multipart(request, em_disco=True)
    if not arquivos:
        return _erro("Escolha uma foto ou um vídeo.")
    legenda = campos.get("legenda", "").strip()[:200]
    _, ctype, arquivo = arquivos[0]
    tipo = _tipo_arquivo(ctype, arquivo)
    if tipo is None:
        _limpar(arquivos)
        return _erro("Esse arquivo não é foto nem vídeo.")

    async def trabalho(progresso=None) -> web.Response:
        bot: Bot = request.app["bot"]
        if tipo == "photo":
            try:
                dados = arquivo.read_bytes() if isinstance(arquivo, Path) else arquivo
                jpg = await asyncio.to_thread(_ajustar_foto, dados)
            except Exception:
                return _erro("Não consegui ler essa imagem.")
            story_id = await db.criar_story("photo", await _subir(bot, user["id"], "photo", jpg, "story.jpg"), None, legenda)
        else:
            r = await _video_do_post(bot, user["id"], arquivo)
            if isinstance(r, str):
                return _erro(r)
            capa, video = r
            story_id = await db.criar_story("video", video, capa, legenda)
        return _ok(id=story_id)

    if tipo == "video":                       # vídeo pode precisar comprimir: não deixa a criadora esperando
        return _em_segundo_plano(request, user, arquivos, "Story", trabalho)
    try:
        return await trabalho()
    finally:
        _limpar(arquivos)


async def apagar_story(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    if not await request.app["db"].apagar_story(int(request.match_info["id"])):
        return _erro("Esse story já saiu do ar.", 404)
    return _ok()


async def vistos_story(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    cfg: Config = request.app["cfg"]
    lista = await request.app["db"].quem_viu_story(int(request.match_info["id"]), cfg.admin_ids)
    return _ok(vistos=[{"id": v["user_id"], "nome": v["nome"] or "", "username": v["username"] or "",
                        "quando": data_local(v["criado_em"], cfg)} for v in lista])


# ════════════════════════════════════════════════════════════════════════════
#  Pedidos personalizados
# ════════════════════════════════════════════════════════════════════════════
async def _pedido_novo(request: web.Request) -> "tuple[object, web.Response | None]":
    db: Database = request.app["db"]
    pedido = await db.get_pedido(int(request.match_info["id"]))
    if not pedido or pedido.status == "AGUARDANDO":
        return None, _erro("Pedido não encontrado.", 404)
    if pedido.status != "NOVO":
        return None, _erro("Esse pedido já foi " + ("entregue." if pedido.status == "ENTREGUE" else "recusado."), 409)
    return pedido, None


async def entregar_pedido(request: web.Request) -> web.Response:
    user = _admin(request, "pedidos")
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    pedido, erro = await _pedido_novo(request)
    if erro:
        return erro
    campos, arquivos = await _ler_multipart(request, em_disco=True)
    if not arquivos:
        return _erro("Escolha pelo menos uma foto ou vídeo para entregar.")
    if len(arquivos) > MAX_ARQUIVOS:
        _limpar(arquivos)
        return _erro(f"Máximo de {MAX_ARQUIVOS} arquivos por entrega.")
    recado = campos.get("mensagem", "").strip()[:500]

    async def trabalho(progresso=None) -> web.Response:
        bot: Bot = request.app["bot"]
        r = await _processar_midias(bot, user["id"], cfg, arquivos, progresso)
        if isinstance(r, web.Response):
            return r
        originais, blur = r
        pack_id = await db.criar_pack(f"✍️ Pedido #{pedido.id}", pedido.preco, originais, blur, tipo="pedido", legenda=recado)
        if not await db.concluir_pedido(pedido.id, "ENTREGUE", recado, pack_id):    # entregue em outra aba ao mesmo tempo
            await db.apagar_pack(pack_id)
            return _erro("Esse pedido já tinha sido concluído.", 409)
        await db.liberar_acesso(pedido.user_id, [pack_id], None)
        entregue = True
        try:
            from webhook import entregar_packs
            await bot.send_message(pedido.user_id, f"🎁 <b>Seu pedido personalizado #{pedido.id} chegou!</b>"
                                   + (f"\n\n💬 {html.escape(recado)}" if recado else ""))
            await entregar_packs(bot, pedido.user_id, [await db.get_pack(pack_id)], f"Pedido #{pedido.id}")
        except Exception:
            log.exception("Pedido %s: não consegui entregar no chat do fã", pedido.id)
            entregue = False                  # continua liberado em Minhas compras na loja
        return _ok(id=pack_id, entregue=True, no_chat=entregue)

    if len(arquivos) > 4 or any(_tipo_arquivo(c, d) == "video" for _, c, d in arquivos):
        return _em_segundo_plano(request, user, arquivos, f"Entrega do pedido #{pedido.id}", trabalho)
    try:
        return await trabalho()
    finally:
        _limpar(arquivos)


async def recusar_pedido(request: web.Request) -> web.Response:
    _admin(request, "pedidos")
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    pedido, erro = await _pedido_novo(request)
    if erro:
        return erro
    recado = str((await request.json()).get("mensagem", "")).strip()[:500]
    if not await db.concluir_pedido(pedido.id, "RECUSADO", recado):
        return _erro("Esse pedido já tinha sido concluído.", 409)
    try:
        await request.app["bot"].send_message(
            pedido.user_id,
            f"😔 {html.escape(cfg.criadora_nome or 'A criadora')} não vai poder fazer seu pedido #{pedido.id}."
            + (f"\n\n💬 {html.escape(recado)}" if recado else "")
            + f"\n\nO valor de <b>{pedido.preco_fmt}</b> será devolvido.")
    except Exception:
        log.warning("Pedido %s recusado: não consegui avisar o fã", pedido.id)
    return _ok()


# ════════════════════════════════════════════════════════════════════════════
#  Meta de mimos
# ════════════════════════════════════════════════════════════════════════════
async def salvar_meta(request: web.Request) -> web.Response:
    _admin(request, "ajustes")
    corpo = await request.json()
    titulo = str(corpo.get("titulo", "")).strip()[:60]
    descricao = str(corpo.get("descricao", "")).strip()[:200]
    alvo = parse_preco(str(corpo.get("alvo", "")))
    if len(titulo) < 2:
        return _erro("Dê um nome para a meta, ex: Câmera nova 📸")
    if not alvo or alvo < 1000:
        return _erro("O valor da meta precisa ser de pelo menos R$ 10,00.")
    if alvo > 100_000_000:
        return _erro("Valor alto demais para uma meta.")
    await request.app["db"].salvar_meta(titulo, descricao, alvo)
    return _ok()


async def encerrar_meta(request: web.Request) -> web.Response:
    _admin(request, "ajustes")
    await request.app["db"].encerrar_meta()
    return _ok()


def meta_json(meta: dict | None) -> dict | None:
    if not meta:
        return None
    return {"titulo": meta["titulo"], "descricao": meta["descricao"] or "", "alvo": meta["alvo"],
            "alvo_fmt": fmt_reais(meta["alvo"]), "arrecadado_fmt": fmt_reais(meta["arrecadado"]),
            "pct": min(100, meta["arrecadado"] * 100 // max(1, meta["alvo"])), "fas": meta["fas"],
            "batida": bool(meta["batida_em"])}


async def _revisao(db: Database) -> bool:
    return await db.get_ajuste("revisao", "0") == "1"


async def ajustes(request: web.Request) -> web.Response:
    _admin(request, "ajustes")
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
    if "pedidos_ativo" in corpo:
        await db.set_ajuste("pedidos_ativo", "1" if corpo["pedidos_ativo"] else "0")
    if "pedidos_min" in corpo:
        minimo = parse_preco(str(corpo["pedidos_min"]))
        if not minimo or minimo > 1_000_000:
            return _erro("Preço mínimo inválido. Use algo como 50,00.")
        await db.set_ajuste("pedidos_min", str(minimo))
    if "pedidos_prazo" in corpo:
        await db.set_ajuste("pedidos_prazo", str(corpo["pedidos_prazo"]).strip()[:40])
    if "pedidos_info" in corpo:
        await db.set_ajuste("pedidos_info", str(corpo["pedidos_info"]).strip()[:400])
    if "carrinho_pct" in corpo:
        try:
            pct = int(str(corpo["carrinho_pct"]).replace("%", "").strip() or 0)
        except ValueError:
            return _erro("Desconto inválido.")
        if not 0 <= pct <= 50:
            return _erro("O desconto do carrinho vai de 0 (desligado) a 50%.")
        await db.set_ajuste("carrinho_pct", str(pct))
    if "carrinho_min" in corpo:
        try:
            minimo = int(corpo["carrinho_min"])
        except (TypeError, ValueError):
            return _erro("Quantidade inválida.")
        if not 2 <= minimo <= 10:
            return _erro("O desconto vale a partir de 2 a 10 packs.")
        await db.set_ajuste("carrinho_min", str(minimo))
    if "gorjetas" in corpo:
        valores = sorted({int(v) for v in str(corpo["gorjetas"]).replace(";", ",").split(",")
                          if v.strip().isdigit() and 1 <= int(v) <= 10000})
        if not valores:
            return _erro("Informe pelo menos um valor, ex: 10, 25, 50, 100")
        await db.set_ajuste("gorjetas", ",".join(map(str, valores[:8])))
    return _ok()


async def config_pedidos(db: Database) -> dict:
    minimo = int(await db.get_ajuste("pedidos_min", "5000") or 5000)
    return {"ativo": await db.get_ajuste("pedidos_ativo", "0") == "1", "min": minimo, "min_fmt": fmt_reais(minimo),
            "prazo": await db.get_ajuste("pedidos_prazo", "") or "", "info": await db.get_ajuste("pedidos_info", "") or ""}


async def config_carrinho(db: Database) -> dict:
    return {"pct": int(await db.get_ajuste("carrinho_pct", "0") or 0),
            "min": int(await db.get_ajuste("carrinho_min", "3") or 3)}


async def config(request: web.Request) -> web.Response:
    """Tudo que a aba Ajustes mostra: planos, cupons, mimos e preferências."""
    _admin(request, "ajustes")
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
        "pedidos": await config_pedidos(db),
        "carrinho": await config_carrinho(db),
        "meta": meta_json(await db.meta_ativa()),
        "bloqueados": [{"id": u["user_id"], "nome": u["nome"] or "", "username": u["username"] or "",
                        "desde": data_local(u["banido_em"], request.app["cfg"]) if u["banido_em"] else ""}
                       for u in await db.listar_banidos()],
    })


async def bloquear(request: web.Request) -> web.Response:
    """Bloqueia (ou desbloqueia) um fã pelo ID do Telegram."""
    _admin(request, "fas")
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
    _admin(request, "fas")
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
    _admin(request, "financeiro")
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
    _admin(request, "financeiro")
    db: Database = request.app["db"]
    if len(await db.listar_planos()) <= 1:
        return _erro("Mantenha pelo menos um plano ativo.")
    await db.desativar_plano(int(request.match_info["id"]))
    return _ok()


async def salvar_cupom(request: web.Request) -> web.Response:
    _admin(request, "financeiro")
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
    _admin(request, "financeiro")
    await request.app["db"].desativar_cupom(request.match_info["codigo"])
    return _ok()


async def aprovar(request: web.Request) -> web.Response:
    """Fila de revisão: publica (ou descarta) um post/pack que estava aguardando."""
    _admin(request, "publicar")
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
    _admin(request, "publicar")
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
    app.router.add_post("/api/criadora/story", novo_story)
    app.router.add_post("/api/criadora/story/{id:\\d+}/apagar", apagar_story)
    app.router.add_get("/api/criadora/story/{id:\\d+}/vistos", vistos_story)
    app.router.add_post("/api/criadora/pedido/{id:\\d+}/entregar", entregar_pedido)
    app.router.add_post("/api/criadora/pedido/{id:\\d+}/recusar", recusar_pedido)
    app.router.add_post("/api/criadora/meta", salvar_meta)
    app.router.add_post("/api/criadora/meta/encerrar", encerrar_meta)
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
