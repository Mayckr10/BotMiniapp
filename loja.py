"""
Mini App da loja (Telegram WebApp): a vitrine abre DENTRO do Telegram, o cliente paga o PIX
na própria página e o bot entrega o conteúdo no chat privado, igual à compra pelo bot.

Rotas (no mesmo servidor aiohttp do webhook, então usam o mesmo HTTPS):
  GET  /loja                       página do Mini App (miniapp/index.html)
  GET  /api/loja/dados             perfil, planos, packs, combos e compras do cliente
  GET  /api/loja/previa/{item_id}  imagem EMBAÇADA do item (nunca a original)
  GET  /api/loja/foto              foto de perfil da criadora
  POST /api/loja/idade             confirmação 18+
  POST /api/loja/comprar           cria o pedido e devolve o PIX (QR + copia e cola)
  GET  /api/loja/pedido/{pix_id}   status do pagamento (?verificar=1 consulta o gateway, "Já paguei")
  POST /api/loja/reenviar          reenvia no chat um pack já comprado
  GET  /api/loja/story/{id}/midia|capa?u&e&s   story no ar (link assinado; só VIP e a criadora)
  POST /api/loja/story/{id}/visto  marca o story como visto
  POST /api/loja/pedido_personalizado  registra o pedido (depois paga em /comprar com pedido_N)
  POST /api/loja/carrinho          fecha o carrinho (depois paga em /comprar com carrinho_N)

Segurança: toda chamada da API manda o `initData` do Telegram no header X-Telegram-Init-Data.
O servidor confere a assinatura HMAC com o token do bot (aiogram.utils.web_app), então sabe
QUEM está comprando sem login, e ninguém consegue se passar por outro usuário.
As mídias originais nunca passam pela página: só o bot as envia, com protect_content.
"""
import base64
import hmac
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from aiogram import Bot
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web

import contas as contas_mod
import criadora as criadora_mod
import galeria
from assinaturas import data_local
from config import Config
import mensagens
from admin_handlers import parse_preco
from database import Carrinho, Database, Pack, fmt_reais
import protecao
from vendas import aplicar_pct, criar_pedido, desconto_primeiro_mes
from webhook import processar_pagamento

log = logging.getLogger(__name__)
PAGINA = Path(__file__).parent / "miniapp" / "index.html"
FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#ff4f8b"/><stop offset="1" stop-color="#ff9a3c"/></linearGradient></defs>'
           '<rect width="64" height="64" rx="16" fill="url(#g)"/><path d="M32 49s-15-9.3-15-20.2C17 23 21 19 26 19c2.9 0 '
           '4.9 1.5 6 3.4 1.1-1.9 3.1-3.4 6-3.4 5 0 9 4 9 9.8C47 39.7 32 49 32 49z" fill="#fff"/></svg>')


async def favicon(_: web.Request) -> web.Response:
    return web.Response(text=FAVICON, content_type="image/svg+xml", headers={"Cache-Control": "public, max-age=604800"})
VALIDADE_INIT_DATA = 24 * 3600
_cache_imagens = galeria._CacheLRU(48 * 1024 * 1024)      # prévias, fotos de posts e stories (limitado: 1 GB de RAM)


def _resumo(packs: list[Pack]) -> str:
    fotos = sum(m["tipo"] == "photo" for p in packs for m in p.originais)
    videos = sum(m["tipo"] == "video" for p in packs for m in p.originais)
    partes = ([f"{fotos} foto{'s' if fotos != 1 else ''}"] if fotos else []) + \
             ([f"{videos} vídeo{'s' if videos != 1 else ''}"] if videos else [])
    return " · ".join(partes)


def _usuario(request: web.Request) -> dict:
    """Valida o initData e devolve o usuário do Telegram. 401 se for inválido ou velho."""
    cfg: Config = request.app["cfg"]
    bruto = request.headers.get("X-Telegram-Init-Data", "")
    if not bruto:                    # navegador: conta logada no painel (cookie de sessão)
        conta = contas_mod.conta_logada(request)
        if conta and conta["telegram_id"]:
            return {"id": conta["telegram_id"], "nome": conta["nome"], "username": None, "web": True}
        raise web.HTTPUnauthorized(text='{"erro":"nao_logado"}', content_type="application/json")
    try:
        dados = safe_parse_webapp_init_data(cfg.bot_token, bruto)
    except ValueError:
        raise web.HTTPUnauthorized(text='{"erro":"init_data_invalido"}', content_type="application/json")
    if not dados.user or time.time() - dados.auth_date.timestamp() > VALIDADE_INIT_DATA:
        raise web.HTTPUnauthorized(text='{"erro":"init_data_expirado"}', content_type="application/json")
    return {"id": dados.user.id, "nome": dados.user.first_name, "username": dados.user.username}


# ── Posts e stories: só para assinantes VIP (e a criadora) ────────────────
# As mídias saem com link assinado para a pessoa (vale ~12h) e o servidor confere o VIP a cada acesso,
# então copiar o link não abre o conteúdo para quem não assina.
def assinar_midia(cfg: Config, user_id: int, ref: str) -> str:
    exp = (int(time.time()) // 3600 + 13) * 3600           # arredondado: o navegador consegue usar o cache
    return f"u={user_id}&e={exp}&s={galeria._assinar(cfg, user_id, ref, 0, exp)}"


async def pode_ver_posts(db: Database, cfg: Config, user_id: int) -> bool:
    return user_id in cfg.admin_ids or user_id in contas_mod.EQUIPE or (await db.assinante_ativo(user_id) and not await db.esta_banido(user_id))


async def checar_midia(request: web.Request, ref: str) -> int:
    """Confere o link assinado e se a pessoa ainda pode ver (VIP ativo). 403 se não puder."""
    cfg: Config = request.app["cfg"]
    try:
        uid, exp, sig = int(request.query["u"]), int(request.query["e"]), request.query["s"]
    except (KeyError, ValueError):
        raise web.HTTPForbidden()
    if exp < time.time() or not hmac.compare_digest(sig, galeria._assinar(cfg, uid, ref, 0, exp)):
        raise web.HTTPForbidden()
    if not await pode_ver_posts(request.app["db"], cfg, uid):
        raise web.HTTPForbidden()
    return uid


async def _idade_ok(db: Database, cfg: Config, user_id: int) -> str | None:
    if not cfg.exigir_maioridade or user_id in cfg.admin_ids:
        return "ok"
    return await db.status_idade(user_id)


async def _imagem(bot: Bot, file_id: str) -> bytes:
    dados = _cache_imagens.get(file_id)
    if dados is None:
        buf = BytesIO()
        await bot.download(file_id, destination=buf)
        dados = buf.getvalue()
        _cache_imagens.put(file_id, dados)
    return dados


# ════════════════════════════════════════════════════════════════════════════
async def pagina(_: web.Request) -> web.FileResponse:
    return web.FileResponse(PAGINA, headers={"Cache-Control": "no-cache"})


async def dados(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    user = _usuario(request)
    if not user.get("web"):
        await db.registrar_usuario(user["id"], user["nome"], user["username"])
    perms = request.app["contas"].permissoes(user["id"])
    financeiro = bool(perms and "financeiro" in perms)

    planos = await db.listar_planos()
    mensal = next((p for p in planos if p.dias == 30), None)

    def economia(p):
        if not mensal or p.dias <= 30:
            return None
        cheio = mensal.preco * p.dias // 30
        return round(100 - p.preco * 100 / cheio) if cheio > p.preco else None

    packs = []
    for p in await db.listar_packs():
        packs.append({"id": p.id, "titulo": p.titulo, "preco_fmt": p.preco_fmt, "preco": p.preco,
                      "resumo": _resumo([p]), "comprado": await db.ja_comprou(user["id"], p.id),
                      "fotos": sum(m["tipo"] == "photo" for m in p.originais),
                      "videos": sum(m["tipo"] == "video" for m in p.originais),
                      "criado_em": p.criado_em, "legenda": p.legenda})
    todos = [p for p in await db.listar_packs(tipo=None) if p.tipo not in ("fila", "agendado", "pedido", "preparando")]
    totais = {"conteudos": len(todos),
              "fotos": sum(m["tipo"] == "photo" for p in todos for m in p.originais),
              "videos": sum(m["tipo"] == "video" for p in todos for m in p.originais)}
    combos = []
    for c in await db.listar_combos():
        itens = await db.packs_do_item(c.id)
        combos.append({"id": c.id, "titulo": c.titulo, "preco_fmt": c.preco_fmt, "preco": c.preco,
                       "resumo": f"{len(itens)} packs · {_resumo(itens)}", "packs": [p.titulo for p in itens],
                       "separados_fmt": fmt_reais(sum(p.preco for p in itens)),
                       "comprado": await db.tem_todos(user["id"], [p.id for p in itens])})
    assinatura = await db.get_assinatura(user["id"])
    info_vip = None
    if assinatura and assinatura.ativa:
        plano = next((p for p in planos if p.id == assinatura.plano_id), None) or mensal or (planos[0] if planos else None)
        restam = (datetime.fromisoformat(assinatura.expira_em) - datetime.now(timezone.utc)).total_seconds() / 86400
        info_vip = {"ativa": True, "ate": data_local(assinatura.expira_em, cfg), "dias": max(0, math.ceil(restam)),
                    "plano": plano.item_id if plano else None, "plano_nome": plano.nome if plano else None,
                    "plano_preco": plano.preco_fmt if plano else None, "plano_dias": plano.dias if plano else None}
    criadora = perms is not None
    ve_posts = criadora or bool(assinatura and assinatura.ativa)
    votos = await db.resultados_enquetes(user["id"])

    def post_json(p: dict) -> dict:
        d = post_basico(cfg, user["id"], p)
        if p.get("enquete"):
            v = votos.get(p["id"], {"votos": {}, "meu": None})
            d["enquete"] = {"opcoes": p["enquete"], "votos": [v["votos"].get(i, 0) for i in range(len(p["enquete"]))],
                            "meu": v["meu"]}
        if p.get("agendado_para"):
            d["agendado_para"] = p["agendado_para"]
        return d

    ag = await db.agendados()
    meus_avisos = await db.avisos_do_usuario(user["id"])
    n_avisos = await db.contar_avisos() if criadora else {}

    def pack_ag(a: dict) -> dict:
        p = a["pack"]
        return {"id": p.id, "titulo": p.titulo, "preco_fmt": p.preco_fmt, "preco": p.preco, "legenda": p.legenda,
                "resumo": _resumo([p]), "fotos": sum(m["tipo"] == "photo" for m in p.originais),
                "videos": sum(m["tipo"] == "video" for m in p.originais), "quando": a["agendado_para"],
                "contagem": a["contagem"], "avisar": p.id in meus_avisos, "avisos": n_avisos.get(p.id, 0),
                "canal": a["postar_canal"]}

    lancamentos = [pack_ag(a) for a in ag["packs"] if a["pack"] and a["contagem"]]
    todos_stories = await db.stories_ativos(user["id"])
    stories = [{"id": st["id"], "tipo": st["tipo"], "legenda": st["legenda"] or "", "criado_em": st["criado_em"],
                "visto": st["visto"], "q": assinar_midia(cfg, user["id"], f"story_{st['id']}"),
                **({"vistos": st["vistos"]} if criadora else {})}
               for st in todos_stories] if ve_posts else []
    publicados = await db.listar_posts()
    cfg_pedidos = await criadora_mod.config_pedidos(db)
    meus_pedidos = [{"id": pd.id, "texto": pd.texto, "valor_fmt": pd.preco_fmt, "status": pd.status, "pack_id": pd.pack_id,
                     "resposta": pd.resposta or "", "quando": data_local(pd.pago_em or pd.criado_em, cfg)}
                    for pd in await db.listar_pedidos(user["id"])]
    meu_rank = None
    if not criadora:
        top = await db.top_fas(datetime.now(timezone.utc) - timedelta(days=30), cfg.admin_ids)
        meu_rank = next(({"pos": i + 1} for i, f in enumerate(top) if f["user_id"] == user["id"]), None)
    promo = {}
    for p in planos:
        pct = await desconto_primeiro_mes(db, user["id"], p)
        if pct:
            promo[p.item_id] = {"pct": pct, "preco_fmt": fmt_reais(aplicar_pct(p.preco, pct))}
    return web.json_response({
        "criadora": {"nome": cfg.criadora_nome,
                     "bio": await db.get_ajuste("bio", "Conteúdo exclusivo todos os dias 🔥"),
                     "tem_foto": bool(await db.get_ajuste("foto_perfil")),
                     "foto_v": (await db.get_ajuste("foto_perfil") or "")[-10:],
                     "totais": totais},
        "usuario": {"nome": user["nome"]},
        "sou_criadora": criadora,
        "papel": "dona" if user["id"] in cfg.admin_ids else "equipe" if criadora else None,
        "permissoes": perms or [],
        "web": bool(user.get("web")),
        "posts": [post_json(p) for p in publicados] if ve_posts else [],
        # Sem VIP não aparece nada do conteúdo na loja: só a quantidade de cada coisa (para o convite de assinar).
        # Os packs continuam à venda pelo canal de amostras e pelo bot; o que já foi comprado fica em Minhas.
        "bloqueio": None if ve_posts else {"posts": len(publicados), "stories": len(todos_stories),
                                           "packs": len(packs) + len(combos), "lancamentos": len(lancamentos)},
        "posts_bloqueados": 0 if ve_posts else len(publicados),
        "stories_bloqueados": 0 if ve_posts else len(todos_stories),
        "lancamentos": lancamentos if ve_posts else [],
        "stories": stories,
        "meta": criadora_mod.meta_json(await db.meta_ativa()),
        "pedidos_cfg": cfg_pedidos if cfg_pedidos["ativo"] or criadora else None,
        "meus_pedidos": meus_pedidos,
        "pedidos_novos": await db.contar_pedidos_novos() if criadora else 0,
        "carrinho_cfg": await criadora_mod.config_carrinho(db),
        "meu_rank": meu_rank,
        "agendados": {"posts": [post_json(p) for p in ag["posts"]],
                      "packs": [pack_ag(a) for a in ag["packs"] if a["pack"]]} if criadora else None,
        "idade": await _idade_ok(db, cfg, user["id"]),
        "teste": await request.app["gateway"].em_teste(),
        "assinatura": info_vip,
        "planos": [{"id": p.item_id, "nome": p.nome, "dias": p.dias, "preco_fmt": p.preco_fmt,
                    "economia": economia(p), "promo": promo.get(p.item_id)} for p in planos],
        "packs": packs if ve_posts else [],
        "combos": combos if ve_posts else [],
        "gorjetas": [v * 100 for v in await mensagens.valores_gorjeta()],
        "minhas": [{"id": p.id, "titulo": p.titulo} for p in await db.packs_do_cliente(user["id"])],
        "fila": await _fila(db, cfg, user["id"]) if criadora else None,     # aguardando revisão da criadora
        "revisao": await db.get_ajuste("revisao", "0") == "1" if criadora else False,
        "curtidas": await db.curtidas_do_feed(user["id"]),                        # {alvo: {n, eu}}
        "mimos": {a: {"qtd": m["qtd"], "total_fmt": fmt_reais(m["total"])}         # só a criadora vê os valores
                  for a, m in (await db.mimos_do_feed()).items()} if financeiro else {},
    })


def post_basico(cfg: Config, user_id: int, p: dict) -> dict:
    d = {"id": p["id"], "texto": p["texto"], "foto": bool(p["foto"]), "video": bool(p.get("video")),
         "criado_em": p["criado_em"]}
    if p["foto"] or p.get("video"):
        d["q"] = assinar_midia(cfg, user_id, f"post_{p['id']}")      # vai na URL da foto/vídeo
    return d


async def _fila(db: Database, cfg: Config, user_id: int) -> dict:
    posts = [post_basico(cfg, user_id, p) for p in await db.listar_posts("fila")]
    packs = [{"id": p.id, "titulo": p.titulo, "preco_fmt": p.preco_fmt, "preco": p.preco, "resumo": _resumo([p]),
              "fotos": sum(m["tipo"] == "photo" for m in p.originais), "videos": sum(m["tipo"] == "video" for m in p.originais),
              "criado_em": p.criado_em, "legenda": p.legenda, "comprado": True}
             for p in await db.listar_packs(tipo="fila")]
    return {"posts": posts, "packs": packs}


async def curtir(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    alvo = str((await request.json()).get("alvo", ""))
    existe = await db.get_post(int(alvo[5:])) if alvo.startswith("post_") and alvo[5:].isdigit() \
        else await db.get_item(alvo) if alvo.startswith(("pack_", "combo_")) else None
    if not existe:
        return web.json_response({"ok": False}, status=404)
    if alvo.startswith("post_") and not await pode_ver_posts(db, request.app["cfg"], user["id"]):
        return web.json_response({"ok": False, "erro": "so_vip"}, status=403)
    curtido, total = await db.curtir(user["id"], alvo)
    return web.json_response({"ok": True, "curtido": curtido, "total": total})


async def votar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    corpo = await request.json()
    try:
        post_id, opcao = int(corpo.get("post_id")), int(corpo.get("opcao"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False}, status=400)
    if not await pode_ver_posts(db, request.app["cfg"], user["id"]):
        return web.json_response({"ok": False, "erro": "so_vip"}, status=403)
    post = next((p for p in await db.listar_posts() if p["id"] == post_id), None)
    if not post or not post["enquete"] or not 0 <= opcao < len(post["enquete"]):
        return web.json_response({"ok": False}, status=404)
    novo = await db.votar(post_id, user["id"], opcao)
    v = (await db.resultados_enquetes(user["id"])).get(post_id, {"votos": {}, "meu": None})
    return web.json_response({"ok": True, "novo": novo, "meu": v["meu"],
                              "votos": [v["votos"].get(i, 0) for i in range(len(post["enquete"]))]})


async def avise(request: web.Request) -> web.Response:
    """Liga/desliga o 'Me avise' de um lançamento com contagem regressiva."""
    db: Database = request.app["db"]
    user = _usuario(request)
    pack_id = str((await request.json()).get("pack_id", ""))
    pack = await db.get_pack(pack_id)
    if not pack or pack.tipo != "agendado":
        return web.json_response({"ok": False, "erro": "Esse lançamento já saiu."}, status=404)
    return web.json_response({"ok": True, "avisar": await db.alternar_aviso(pack_id, user["id"])})


async def previa(request: web.Request) -> web.Response:
    """Só a amostra embaçada: o arquivo original nunca é servido aqui."""
    db: Database = request.app["db"]
    packs = await db.packs_do_item(request.match_info["item_id"])
    capa = next((p.blur[0]["file_id"] for p in packs if p.blur), None)
    if not capa:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], capa), content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=3600"})


async def foto(request: web.Request) -> web.Response:
    file_id = await request.app["db"].get_ajuste("foto_perfil")
    if not file_id:
        raise web.HTTPNotFound()
    return web.Response(body=await _imagem(request.app["bot"], file_id), content_type="image/jpeg")


async def idade(request: web.Request) -> web.Response:
    user = _usuario(request)
    corpo = await request.json()
    await request.app["db"].registrar_idade(user["id"], maior=bool(corpo.get("maior")))
    return web.json_response({"ok": True, "idade": "ok" if corpo.get("maior") else "menor"})


async def comprar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    user = _usuario(request)
    corpo = await request.json()
    status_idade = await _idade_ok(db, cfg, user["id"])
    if status_idade != "ok":
        return web.json_response({"status": "idade", "idade": status_idade}, status=403)

    pedido = await criar_pedido(db, request.app["gateway"], cfg, user["id"], str(corpo.get("item_id", "")),
                                (corpo.get("cupom") or "").strip() or None)
    if pedido.status == "inexistente":
        return web.json_response({"status": "inexistente"}, status=404)
    if pedido.status == "ja_comprou":
        enviou, restantes = await protecao.reenviar(bot, db, cfg, user["id"], pedido.packs, pedido.item.titulo)
        return web.json_response({"status": "ja_comprou" if enviou else "limite", "restantes": restantes})
    if pedido.status == "erro_gateway":
        return web.json_response({"status": "erro_gateway"}, status=502)

    c = pedido.cobranca
    return web.json_response({
        "status": "ok",
        "titulo": pedido.item.titulo,
        "pix_id": c.pix_id,
        "copia_e_cola": c.copia_e_cola,
        "qr": "data:image/png;base64," + base64.b64encode(c.qr_png).decode(),
        "valor_fmt": fmt_reais(pedido.preco),
        "original_fmt": fmt_reais(pedido.item.preco_cheio) if isinstance(pedido.item, Carrinho) and
                        pedido.item.preco_cheio > pedido.preco else
                        pedido.item.preco_fmt if pedido.preco != pedido.item.preco else None,
        "cupom": {"codigo": pedido.cupom.codigo, "descricao": pedido.cupom.descricao} if pedido.cupom else None,
        "cupom_erro": f"Cupom {pedido.cupom_invalido} não aplicado: {pedido.motivo_cupom}"
                      if pedido.cupom_invalido else None,
        "promo": pedido.promo or (f"Desconto do carrinho: {fmt_reais(pedido.item.preco_cheio - pedido.item.preco)} OFF"
                                  if isinstance(pedido.item, Carrinho) and pedido.item.preco_cheio > pedido.item.preco else None),
        "expira": pedido.expira.isoformat(),
    })


async def pedido(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    pix_id = request.match_info["pix_id"]
    venda = await db.get_venda_por_pix(pix_id)
    if not venda or venda.user_id != user["id"]:
        raise web.HTTPNotFound()
    if venda.status != "PAGO" and request.query.get("verificar"):
        await processar_pagamento(request.app["bot"], db, request.app["gateway"], pix_id, request.app["cfg"])
        venda = await db.get_venda_por_pix(pix_id)
    return web.json_response({"status": venda.status})


async def teste_pagar(request: web.Request) -> web.Response:
    """Só com PIX_GATEWAY=mock: simula o pagamento pela loja (testar pelo celular)."""
    gw = request.app["gateway"]
    user = _usuario(request)
    pix_id = str((await request.json()).get("pix_id", ""))
    venda = await request.app["db"].get_venda_por_pix(pix_id)
    if not venda or venda.user_id != user["id"] or not await gw.marcar_pago_teste(pix_id):
        raise web.HTTPNotFound()
    await processar_pagamento(request.app["bot"], request.app["db"], gw, pix_id, request.app["cfg"])
    return web.json_response({"ok": True})


async def reenviar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    pack_id = str((await request.json()).get("pack_id", ""))
    pack = await db.get_pack(pack_id)
    if not pack or not await db.ja_comprou(user["id"], pack_id):
        raise web.HTTPForbidden()
    enviou, restantes = await protecao.reenviar(request.app["bot"], db, request.app["cfg"], user["id"], [pack], pack.titulo)
    if not enviou:
        return web.json_response({"ok": False, "erro": "limite", "limite": await protecao.limite_reenvio(db)}, status=429)
    return web.json_response({"ok": True, "restantes": restantes})


# ── Stories ────────────────────────────────────────────────────────────────
async def story_midia(request: web.Request) -> web.Response:
    """Foto ou vídeo do story enquanto ele está no ar (some sozinho em 24h)."""
    db: Database = request.app["db"]
    st = await db.get_story(int(request.match_info["id"]))
    if not st:
        raise web.HTTPNotFound()
    await checar_midia(request, f"story_{st['id']}")
    bot: Bot = request.app["bot"]
    if request.match_info["parte"] == "capa" or st["tipo"] == "photo":
        file_id = st["capa"] if request.match_info["parte"] == "capa" and st["tipo"] == "video" else st["file_id"]
        return web.Response(body=await _imagem(bot, file_id), content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=3600"})
    try:
        dados = await galeria._baixar(bot, st["file_id"])
    except Exception:
        log.exception("Story %s: não consegui baixar o vídeo", st["id"])
        raise web.HTTPNotFound()
    resp = galeria._resposta_com_range(request, dados, "video/mp4")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


async def story_visto(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    story_id = int(request.match_info["id"])
    if (user["id"] not in request.app["cfg"].admin_ids and await db.get_story(story_id)
            and await pode_ver_posts(db, request.app["cfg"], user["id"])):
        await db.marcar_story_visto(story_id, user["id"])
    return web.json_response({"ok": True})


# ── Pedido personalizado ──────────────────────────────────────────────────
MAX_PEDIDO = 1_000_000        # R$ 10.000,00


async def pedido_personalizado(request: web.Request) -> web.Response:
    """Registra o pedido (aguardando PIX). Depois o app chama /comprar com o item_id devolvido."""
    db: Database = request.app["db"]
    cfg: Config = request.app["cfg"]
    user = _usuario(request)
    if await _idade_ok(db, cfg, user["id"]) != "ok":
        return web.json_response({"ok": False, "erro": "Confirme sua idade antes."}, status=403)
    conf = await criadora_mod.config_pedidos(db)
    if not conf["ativo"]:
        return web.json_response({"ok": False, "erro": "Os pedidos personalizados estão fechados agora."}, status=403)
    corpo = await request.json()
    texto = " ".join(str(corpo.get("texto", "")).split())[:1000]
    valor = parse_preco(str(corpo.get("valor", "")))
    if len(texto) < 10:
        return web.json_response({"ok": False, "erro": "Descreva o pedido com um pouco mais de detalhe."}, status=400)
    if not valor or valor < conf["min"]:
        return web.json_response({"ok": False, "erro": f"O valor mínimo é {conf['min_fmt']}."}, status=400)
    if valor > MAX_PEDIDO:
        return web.json_response({"ok": False, "erro": f"O valor máximo é {fmt_reais(MAX_PEDIDO)}."}, status=400)
    if await db.pedidos_aguardando_recentes(user["id"]) >= 5:
        return web.json_response({"ok": False, "erro": "Você já tem pedidos aguardando pagamento. Pague um deles ou espere um pouco."},
                                 status=429)
    pedido_id = await db.criar_pedido_personalizado(user["id"], texto, valor)
    return web.json_response({"ok": True, "item_id": f"pedido_{pedido_id}"})


# ── Carrinho ──────────────────────────────────────────────────────────────
async def carrinho(request: web.Request) -> web.Response:
    """Fecha o carrinho: confere os packs, aplica o desconto e devolve o item_id para pagar num PIX só."""
    db: Database = request.app["db"]
    user = _usuario(request)
    corpo = await request.json()
    ids = corpo.get("pack_ids")
    if not isinstance(ids, list) or not ids:
        return web.json_response({"ok": False, "erro": "Carrinho vazio."}, status=400)
    ids = list(dict.fromkeys(str(i) for i in ids))[:30]
    validos, fora = [], []
    for pid in ids:
        p = await db.get_pack(pid)
        if not p or p.tipo != "pack" or await db.ja_comprou(user["id"], pid):
            fora.append(pid)
        else:
            validos.append(p)
    if fora:          # algo saiu da vitrine ou já foi comprado: o app tira do carrinho e mostra de novo
        return web.json_response({"ok": False, "erro": "Alguns itens do carrinho não estão mais disponíveis.",
                                  "remover": fora}, status=409)
    if len(validos) == 1:
        return web.json_response({"ok": True, "item_id": validos[0].id})
    conf = await criadora_mod.config_carrinho(db)
    cheio = sum(p.preco for p in validos)
    preco = cheio - cheio * conf["pct"] // 100 if conf["pct"] and len(validos) >= conf["min"] else cheio
    carrinho_id = await db.criar_carrinho(user["id"], [p.id for p in validos], max(100, preco), cheio)
    return web.json_response({"ok": True, "item_id": f"carrinho_{carrinho_id}"})


@web.middleware
async def barrar_bloqueados(request: web.Request, handler):
    """Fã bloqueado pela criadora não usa a API da loja (as imagens públicas continuam como estão)."""
    if request.path.startswith("/api/") and request.headers.get("X-Telegram-Init-Data"):
        try:
            user = _usuario(request)
        except web.HTTPException:
            return await handler(request)          # a própria rota responde o 401
        if user["id"] not in request.app["cfg"].admin_ids and await request.app["db"].esta_banido(user["id"]):
            return web.json_response({"ok": False, "erro": "bloqueado"}, status=403)
    return await handler(request)


def registrar_rotas(app: web.Application) -> None:
    app.middlewares.append(barrar_bloqueados)
    app.router.add_get("/loja", pagina)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/api/loja/dados", dados)
    app.router.add_get("/api/loja/previa/{item_id}", previa)
    app.router.add_get("/api/loja/foto", foto)
    app.router.add_post("/api/loja/idade", idade)
    app.router.add_post("/api/loja/curtir", curtir)
    app.router.add_post("/api/loja/votar", votar)
    app.router.add_post("/api/loja/avise", avise)
    app.router.add_post("/api/loja/comprar", comprar)
    app.router.add_get("/api/loja/pedido/{pix_id}", pedido)
    app.router.add_post("/api/loja/reenviar", reenviar)
    app.router.add_post("/api/loja/teste_pagar", teste_pagar)
    app.router.add_get("/api/loja/story/{id:\\d+}/{parte:midia|capa}", story_midia)
    app.router.add_post("/api/loja/story/{id:\\d+}/visto", story_visto)
    app.router.add_post("/api/loja/pedido_personalizado", pedido_personalizado)
    app.router.add_post("/api/loja/carrinho", carrinho)
    galeria.registrar_rotas(app)


def url_loja(cfg: Config) -> str | None:
    """O Telegram só abre Mini Apps em HTTPS (o simulador aceita http)."""
    base = cfg.public_base_url.rstrip("/")
    if cfg.loja_ativa and (base.startswith("https://") or cfg.telegram_api_base):
        return f"{base}/loja"
    return None

