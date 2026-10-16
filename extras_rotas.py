"""
Rotas HTTP das melhorias opcionais (as regras ficam em extras.py, os ajustes em recursos.py).

Fã (Mini App, mesmo login da loja):
  POST /api/loja/teste_vip                 começa o teste grátis do VIP
  GET  /api/loja/carteira                  saldo, valores de recarga e extrato
  POST /api/loja/pergunta                  registra a pergunta (depois paga em /comprar com pergunta_N)
  GET  /api/loja/comentarios/{alvo}        comentários aprovados (e os meus pendentes)
  POST /api/loja/comentar                  novo comentário
  POST /api/loja/sorteio/{id}/participar
  POST /api/loja/avaliar                   nota de 1 a 5 num pack comprado
  POST /api/loja/silenciar                 liga/desliga os avisos de conteúdo novo
  GET  /api/loja/teaser/{pack_id}?u&e&s    teaser em vídeo (link assinado)

Criadora e equipe (/api/criadora/x/...): perguntas, comentários, sorteios, links, relatório, vazamento,
backup, coleções e promoção relâmpago. Cada rota exige a permissão da equipe que combina com ela.
"""
import hmac
import html
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web

import extras
import galeria
import recursos as R
from criadora import _admin, _erro, _ok, _quando
from database import STATUS_PAGO, Database, _agora, agora, fmt_reais, iso

log = logging.getLogger(__name__)


def _usuario(request: web.Request) -> dict:
    from loja import _usuario as u
    return u(request)


async def _json(request: web.Request) -> dict:
    try:
        corpo = await request.json()
        return corpo if isinstance(corpo, dict) else {}
    except Exception:
        return {}


def _nome_curto(u: dict | None) -> str:
    return ((u or {}).get("nome") or "Fã").split(" ")[0][:20]


# ════════════════════════════════════════════════════════════════════════════
#  Dados das melhorias que vão junto com /api/loja/dados
# ════════════════════════════════════════════════════════════════════════════
async def dados_loja(request: web.Request, user: dict, criadora: bool, ve_posts: bool, packs: list[dict]) -> dict:
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    bot = request.app["bot"]
    uid = user["id"]
    c = await R.varios(db, "teste_ativo", "perguntas_ativo", "perguntas_preco", "perguntas_resposta", "perguntas_info",
                       "carteira_ativo", "comentarios_ativo", "comentarios_moderar", "sorteios_ativo", "novidades_ativo",
                       "aval_ativo", "aval_min", "indic_ativo", "indic_recompensa", "indic_amigo_pct", "indic_quando",
                       "colecoes_ativo", "teaser_ativo", "relampago_ativo", "ppv_ativo", "upsell_ativo",
                       "tema_cor", "tema_modo")
    r: dict = {"tema": {"cor": c["tema_cor"], "modo": c["tema_modo"]}}

    # Packs: promoção relâmpago, nota, coleção, teaser
    ids = [p["id"] for p in packs]
    if ids:
        marcas = ",".join("?" * len(ids))
        notas = {x["pack_id"]: (x["media"], x["n"]) for x in await db._todos(
            f"SELECT pack_id, AVG(nota) AS media, COUNT(*) AS n FROM avaliacoes WHERE pack_id IN ({marcas}) GROUP BY pack_id", ids)}
        minhas = {x["pack_id"]: x["nota"] for x in await db._todos(
            f"SELECT pack_id, nota FROM avaliacoes WHERE user_id = ? AND pack_id IN ({marcas})", [uid, *ids])}
        for p in packs:
            pk = await db.get_pack(p["id"])
            if pk and pk.em_promo:
                p["promo"] = {"pct": pk.promo_pct, "ate": pk.promo_ate, "preco_fmt": fmt_reais(pk.preco_atual)}
            if pk and pk.colecao_id:
                p["colecao_id"] = pk.colecao_id
            if c["aval_ativo"]:
                media, n = notas.get(p["id"], (None, 0))
                if n >= max(1, c["aval_min"]):
                    p["nota"] = {"media": round(media, 1), "n": n}
                if p["id"] in minhas:
                    p["minha_nota"] = minhas[p["id"]]
            if c["teaser_ativo"] and pk and any(m["tipo"] == "video" for m in pk.originais):
                from loja import assinar_midia
                p["teaser"] = assinar_midia(cfg, uid, f"teaser_{p['id']}")

    if c["colecoes_ativo"] or criadora:
        r["colecoes"] = [dict(x) for x in await db._todos("SELECT id, nome FROM colecoes ORDER BY ordem, id")]
        r["colecoes_ativo"] = c["colecoes_ativo"]

    dias_teste = await extras.teste_disponivel(db, uid) if not criadora else 0
    if dias_teste:
        r["teste_vip"] = dias_teste

    if c["perguntas_ativo"]:
        r["perguntas"] = {"preco_fmt": fmt_reais(c["perguntas_preco"]), "onde": c["perguntas_resposta"],
                          "info": c["perguntas_info"],
                          "minhas": [{"id": x["id"], "texto": x["texto"], "status": x["status"], "resposta": x["resposta"] or "",
                                      "onde": x["onde"]} for x in await db._todos(
                              "SELECT * FROM perguntas WHERE user_id = ? AND status != 'AGUARDANDO' ORDER BY id DESC LIMIT 10", (uid,))]}
    if c["carteira_ativo"]:
        r["carteira"] = {"saldo": await extras.saldo(db, uid), "saldo_fmt": fmt_reais(await extras.saldo(db, uid)),
                         "valores": [v * 100 for v in await R.lista(db, "carteira_valores")],
                         "bonus": await R.valor(db, "carteira_bonus"),
                         "bonus_min_fmt": fmt_reais(await R.valor(db, "carteira_bonus_min"))}
    n = await extras.nivel(db, uid) if not criadora else None
    if n:
        r["nivel"] = n
    if c["comentarios_ativo"]:
        rows = await db._todos("SELECT alvo, COUNT(*) AS n FROM comentarios WHERE status = 'aprovado' GROUP BY alvo")
        r["comentarios"] = {"contagem": {x["alvo"]: x["n"] for x in rows}, "moderar": c["comentarios_moderar"]}
        if criadora:
            r["comentarios"]["pendentes"] = (await db._um("SELECT COUNT(*) AS n FROM comentarios WHERE status = 'pendente'"))["n"]
    if c["sorteios_ativo"]:
        sorteios = []
        for s in await db._todos("SELECT * FROM sorteios WHERE status = 'ATIVO' ORDER BY fim_em"):
            s = dict(s)
            part = await db._um("SELECT 1 FROM sorteio_part WHERE sorteio_id = ? AND user_id = ?", (s["id"], uid))
            total = (await db._um("SELECT COUNT(*) AS n FROM sorteio_part WHERE sorteio_id = ?", (s["id"],)))["n"]
            sorteios.append({"id": s["id"], "titulo": s["titulo"], "premio": s["premio"], "publico": s["publico"],
                             "fim": s["fim_em"], "participo": bool(part), "participantes": total,
                             "posso": criadora or await extras.pode_participar(db, cfg, s, uid)})
        r["sorteios"] = sorteios
    if c["novidades_ativo"]:
        u = await db._um("SELECT silenciado FROM usuarios WHERE user_id = ?", (uid,))
        r["novidades"] = {"silenciado": bool(u and u["silenciado"])}
    if c["aval_ativo"]:
        r["avaliacoes"] = True
    if c["indic_ativo"] and not criadora:
        try:
            eu = await bot.me()
            r["indicacao"] = {"link": f"https://t.me/{eu.username}?start=r_{uid}",
                              "recompensa_fmt": fmt_reais(c["indic_recompensa"]), "amigo_pct": c["indic_amigo_pct"],
                              "quando": c["indic_quando"], "carteira": c["carteira_ativo"],
                              "amigos": (await db._um("SELECT COUNT(*) AS n FROM usuarios WHERE indicado_por = ?", (uid,)))["n"],
                              "pagos": (await db._um("SELECT COUNT(*) AS n FROM usuarios WHERE indicado_por = ? "
                                                     "AND indicacao_paga = 1", (uid,)))["n"]}
        except Exception:
            pass
    if c["upsell_ativo"] and not criadora:
        up = await extras.upsell_ativo(db, uid)
        if up:
            r["upsell"] = up
    r["relampago"] = c["relampago_ativo"]
    r["ppv"] = c["ppv_ativo"]
    return r


# ════════════════════════════════════════════════════════════════════════════
#  Fã
# ════════════════════════════════════════════════════════════════════════════
async def teste_vip(request: web.Request) -> web.Response:
    user = _usuario(request)
    erro = await extras.ativar_teste(request.app["bot"], request.app["db"], request.app["cfg"], user["id"])
    return _erro(erro) if erro else _ok()


async def carteira(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    if not await R.valor(db, "carteira_ativo"):
        return _erro("A carteira está desligada.", 404)
    nomes = {"recarga": "Recarga", "bonus": "Bônus", "compra": "Compra", "indicacao": "Indicação de amigo",
             "estorno": "Devolução", "ajuste": "Ajuste"}
    cfg = request.app["cfg"]
    from assinaturas import data_local
    mov = [{"valor_fmt": ("+" if m["valor"] > 0 else "−") + fmt_reais(abs(m["valor"])), "positivo": m["valor"] > 0,
            "tipo": nomes.get(m["tipo"], m["tipo"]), "quando": data_local(m["criado_em"], cfg)}
           for m in await extras.extrato(db, user["id"])]
    s = await extras.saldo(db, user["id"])
    return _ok(saldo=s, saldo_fmt=fmt_reais(s), extrato=mov)


async def nova_pergunta(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    c = await R.varios(db, "perguntas_ativo", "perguntas_preco", "perguntas_resposta")
    if not c["perguntas_ativo"]:
        return _erro("A caixinha de perguntas está fechada.", 404)
    corpo = await _json(request)
    texto = " ".join(str(corpo.get("texto", "")).split())[:500]
    if len(texto) < 3:
        return _erro("Escreva sua pergunta.")
    onde = c["perguntas_resposta"]
    if onde == "fa_escolhe":
        onde = "privado" if corpo.get("onde") == "privado" else "feed"
    # pergunta não paga anterior: reaproveita (evita lixo se o fã desistir e tentar de novo)
    await db.conn.execute("DELETE FROM perguntas WHERE user_id = ? AND status = 'AGUARDANDO' AND criado_em < ?",
                          (user["id"], iso(agora() - timedelta(days=1))))
    cur = await db.conn.execute("INSERT INTO perguntas (user_id, texto, valor, onde, criado_em) VALUES (?, ?, ?, ?, ?)",
                                (user["id"], texto, c["perguntas_preco"], onde, _agora()))
    await db.conn.commit()
    return _ok(item_id=f"pergunta_{cur.lastrowid}")


async def _pode_comentar(db: Database, cfg, request, uid: int, alvo: str) -> bool:
    if request.app["contas"].permissoes(uid) is not None:
        return True
    if await db.esta_banido(uid):
        return False
    if alvo.startswith("post_"):
        return await db.assinante_ativo(uid)
    return await db.ja_comprou(uid, alvo) or await db.assinante_ativo(uid)


async def comentarios(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    alvo = request.match_info["alvo"]
    if not await R.valor(db, "comentarios_ativo"):
        return _erro("Comentários desligados.", 404)
    sou_casa = request.app["contas"].permissoes(user["id"]) is not None
    rows = await db._todos(
        "SELECT c.id, c.user_id, c.texto, c.status, c.criado_em, u.nome FROM comentarios c "
        "LEFT JOIN usuarios u ON u.user_id = c.user_id WHERE c.alvo = ? AND (c.status = 'aprovado' "
        "OR (c.status = 'pendente' AND c.user_id = ?)) ORDER BY c.id LIMIT 200", (alvo, user["id"]))
    casa = set(request.app["cfg"].admin_ids)
    return _ok(itens=[{"id": r["id"], "texto": r["texto"], "pendente": r["status"] == "pendente",
                       "nome": request.app["cfg"].criadora_nome if r["user_id"] in casa else _nome_curto(dict(r)),
                       "criadora": r["user_id"] in casa, "meu": r["user_id"] == user["id"], "quando": r["criado_em"]}
                      for r in rows], pode_apagar=sou_casa)


async def comentar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    user = _usuario(request)
    if not await R.valor(db, "comentarios_ativo"):
        return _erro("Comentários desligados.", 404)
    corpo = await _json(request)
    alvo = str(corpo.get("alvo", ""))[:40]
    texto = " ".join(str(corpo.get("texto", "")).split())[:500]
    if not texto:
        return _erro("Escreva o comentário.")
    if not (alvo.startswith("post_") and alvo[5:].isdigit()) and not await db.get_pack(alvo):
        return _erro("Não encontrei essa publicação.", 404)
    if not await _pode_comentar(db, cfg, request, user["id"], alvo):
        return _erro("Só assinantes (ou quem comprou) podem comentar aqui.", 403)
    recentes = (await db._um("SELECT COUNT(*) AS n FROM comentarios WHERE user_id = ? AND criado_em > ?",
                             (user["id"], iso(agora() - timedelta(hours=1)))))["n"]
    if recentes >= 15:
        return _erro("Muitos comentários seguidos. Tente daqui a pouco.", 429)
    casa = request.app["contas"].permissoes(user["id"]) is not None
    status = "aprovado" if casa or not await R.valor(db, "comentarios_moderar") else "pendente"
    await db.conn.execute("INSERT INTO comentarios (alvo, user_id, texto, status, criado_em) VALUES (?, ?, ?, ?, ?)",
                          (alvo, user["id"], texto, status, _agora()))
    await db.conn.commit()
    return _ok(pendente=status == "pendente")


async def participar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    s = await db._um("SELECT * FROM sorteios WHERE id = ? AND status = 'ATIVO'", (int(request.match_info["id"]),))
    if not s or not await R.valor(db, "sorteios_ativo"):
        return _erro("Esse sorteio já terminou.", 404)
    if not await extras.pode_participar(db, request.app["cfg"], dict(s), user["id"]):
        return _erro({"vip": "Esse sorteio é só para assinantes do VIP.",
                      "compradores": "Esse sorteio é para quem já comprou algum conteúdo."}.get(s["publico"], "Você não pode participar."), 403)
    await db.conn.execute("INSERT OR IGNORE INTO sorteio_part VALUES (?, ?, ?)", (s["id"], user["id"], _agora()))
    await db.conn.commit()
    return _ok()


async def avaliar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    if not await R.valor(db, "aval_ativo"):
        return _erro("Avaliações desligadas.", 404)
    corpo = await _json(request)
    pack_id, nota = str(corpo.get("pack_id", "")), corpo.get("nota")
    if nota not in (1, 2, 3, 4, 5):
        return _erro("Escolha de 1 a 5 estrelas.")
    if not await db.ja_comprou(user["id"], pack_id):
        return _erro("Só quem comprou pode avaliar.", 403)
    await db.conn.execute("INSERT INTO avaliacoes VALUES (?, ?, ?, ?) ON CONFLICT(user_id, pack_id) DO UPDATE SET "
                          "nota = excluded.nota, criado_em = excluded.criado_em", (user["id"], pack_id, nota, _agora()))
    await db.conn.commit()
    return _ok()


async def silenciar(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    user = _usuario(request)
    corpo = await _json(request)
    await db.conn.execute("UPDATE usuarios SET silenciado = ? WHERE user_id = ?", (1 if corpo.get("silenciar") else 0, user["id"]))
    await db.conn.commit()
    return _ok()


async def teaser(request: web.Request) -> web.Response:
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    pack_id = request.match_info["pack_id"]
    try:
        uid, exp, sig = int(request.query["u"]), int(request.query["e"]), request.query["s"]
    except (KeyError, ValueError):
        raise web.HTTPForbidden()
    if exp < time.time() or not hmac.compare_digest(sig, galeria._assinar(cfg, uid, f"teaser_{pack_id}", 0, exp)):
        raise web.HTTPForbidden()
    if await db.esta_banido(uid):
        raise web.HTTPForbidden()
    pack = await db.get_pack(pack_id)
    if not pack or pack.tipo not in ("pack", "ppv"):
        raise web.HTTPNotFound()
    caminho = await extras.teaser(request.app["bot"], db, pack)
    if not caminho:
        raise web.HTTPServiceUnavailable(text='{"erro":"sem_teaser"}', content_type="application/json")
    return web.FileResponse(caminho, headers={"Cache-Control": "private, max-age=86400", "Content-Type": "video/mp4"})


# ════════════════════════════════════════════════════════════════════════════
#  Criadora e equipe
# ════════════════════════════════════════════════════════════════════════════
async def perguntas_lista(request: web.Request) -> web.Response:
    _admin(request, "pedidos")
    db: Database = request.app["db"]
    from assinaturas import data_local
    rows = await db._todos("SELECT p.*, u.nome, u.username FROM perguntas p LEFT JOIN usuarios u ON u.user_id = p.user_id "
                           "WHERE p.status != 'AGUARDANDO' ORDER BY CASE p.status WHEN 'NOVA' THEN 0 ELSE 1 END, p.id DESC LIMIT 60")
    return _ok(itens=[{"id": r["id"], "texto": r["texto"], "status": r["status"], "resposta": r["resposta"] or "",
                       "valor_fmt": fmt_reais(r["valor"]), "onde": r["onde"], "fa": r["nome"] or "Fã",
                       "username": r["username"] or "", "user_id": r["user_id"],
                       "quando": data_local(r["pago_em"] or r["criado_em"], request.app["cfg"])} for r in rows])


async def pergunta_acao(request: web.Request) -> web.Response:
    _admin(request, "pedidos")
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    bot = request.app["bot"]
    pid, acao = int(request.match_info["id"]), request.match_info["acao"]
    p = await db._um("SELECT * FROM perguntas WHERE id = ? AND status = 'NOVA'", (pid,))
    if not p:
        return _erro("Essa pergunta já foi respondida ou recusada.", 404)
    corpo = await _json(request)
    if acao == "responder":
        resposta = str(corpo.get("resposta", "")).strip()[:3000]
        if not resposta:
            return _erro("Escreva a resposta.")
        post_id = None
        if p["onde"] == "feed":
            quem = "Um fã" if await R.valor(db, "perguntas_anonimo") else _nome_curto(await db.get_usuario(p["user_id"]))
            post_id = await db.criar_post(f"❓ {quem} perguntou: {p['texto']}\n\n💬 {resposta}")
        cur = await db.conn.execute("UPDATE perguntas SET status = 'RESPONDIDA', resposta = ?, post_id = ?, respondida_em = ? "
                                    "WHERE id = ? AND status = 'NOVA'", (resposta, post_id, _agora(), pid))
        await db.conn.commit()
        if cur.rowcount != 1:
            return _erro("Essa pergunta já foi respondida.", 409)
        try:
            if p["onde"] == "feed":
                await bot.send_message(p["user_id"], f"💬 <b>{html.escape(cfg.criadora_nome)} respondeu sua pergunta!</b>\n"
                                                     "A resposta saiu no feed do VIP.",
                                       reply_markup=extras.InlineKeyboardMarkup(inline_keyboard=extras.teclado_loja(cfg, "Ver no feed")) or None)
            else:
                await bot.send_message(p["user_id"], f"💬 <b>{html.escape(cfg.criadora_nome)} respondeu:</b>\n\n"
                                                     f"<i>{html.escape(p['texto'])}</i>\n\n{html.escape(resposta)}",
                                       protect_content=True)
        except Exception:
            pass
        return _ok(post_id=post_id)
    # recusar: devolve na carteira (se ligada) ou avisa que a devolução é manual
    devolvido = False
    if await R.valor(db, "carteira_ativo"):
        await extras.creditar(db, p["user_id"], p["valor"], "estorno", f"pergunta_{pid}")
        devolvido = True
    await db.conn.execute("UPDATE perguntas SET status = 'RECUSADA', respondida_em = ? WHERE id = ?", (_agora(), pid))
    await db.conn.commit()
    try:
        await bot.send_message(p["user_id"], "❓ Sua pergunta não pôde ser respondida desta vez. "
                                             + (f"Devolvi {fmt_reais(p['valor'])} na sua carteira da loja." if devolvido
                                                else "A criadora vai combinar a devolução com você."))
    except Exception:
        pass
    return _ok(devolvido=devolvido, valor_fmt=fmt_reais(p["valor"]), user_id=p["user_id"])


async def comentarios_pendentes(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    db: Database = request.app["db"]
    rows = await db._todos("SELECT c.*, u.nome, u.username FROM comentarios c LEFT JOIN usuarios u ON u.user_id = c.user_id "
                           "WHERE c.status = 'pendente' ORDER BY c.id LIMIT 100")
    itens = []
    for r in rows:
        alvo = r["alvo"]
        if alvo.startswith("post_"):
            post = await db.get_post(int(alvo[5:]))
            onde = "Post: " + ((post or {}).get("texto") or "foto")[:40]
        else:
            pack = await db.get_pack(alvo)
            onde = "Pack: " + (pack.titulo if pack else alvo)
        itens.append({"id": r["id"], "texto": r["texto"], "fa": r["nome"] or "Fã", "username": r["username"] or "",
                      "user_id": r["user_id"], "onde": onde})
    return _ok(itens=itens)


async def comentario_acao(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    db: Database = request.app["db"]
    cid, acao = int(request.match_info["id"]), request.match_info["acao"]
    if acao == "apagar":
        await db.conn.execute("DELETE FROM comentarios WHERE id = ?", (cid,))
    else:
        await db.conn.execute("UPDATE comentarios SET status = ? WHERE id = ?",
                              ("aprovado" if acao == "aprovar" else "recusado", cid))
    await db.conn.commit()
    return _ok()


async def sorteios_lista(request: web.Request) -> web.Response:
    _admin(request, "fas")
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    from assinaturas import data_local
    itens = []
    for s in await db._todos("SELECT * FROM sorteios ORDER BY CASE status WHEN 'ATIVO' THEN 0 ELSE 1 END, id DESC LIMIT 30"):
        n = (await db._um("SELECT COUNT(*) AS n FROM sorteio_part WHERE sorteio_id = ?", (s["id"],)))["n"]
        g = await db.get_usuario(s["ganhador"]) if s["ganhador"] else None
        itens.append({"id": s["id"], "titulo": s["titulo"], "premio": s["premio"], "publico": s["publico"],
                      "status": s["status"], "fim": data_local(s["fim_em"], cfg), "participantes": n,
                      "ganhador": ({"id": s["ganhador"], "nome": (g or {}).get("nome") or "Fã",
                                    "username": (g or {}).get("username") or ""} if s["ganhador"] else None)})
    return _ok(itens=itens)


async def sorteio_novo(request: web.Request) -> web.Response:
    _admin(request, "fas")
    db: Database = request.app["db"]
    corpo = await _json(request)
    titulo, premio = str(corpo.get("titulo", "")).strip()[:80], str(corpo.get("premio", "")).strip()[:200]
    publico = corpo.get("publico") if corpo.get("publico") in ("vip", "compradores", "todos") else "vip"
    if not titulo or not premio:
        return _erro("Preencha o nome e o prêmio.")
    quando, erro = _quando(corpo)
    if erro or not quando:
        return _erro(erro or "Escolha quando o sorteio acaba.")
    cur = await db.conn.execute("INSERT INTO sorteios (titulo, premio, publico, fim_em, criado_em) VALUES (?, ?, ?, ?, ?)",
                                (titulo, premio, publico, iso(quando), _agora()))
    await db.conn.commit()
    return _ok(id=cur.lastrowid)


async def sorteio_acao(request: web.Request) -> web.Response:
    _admin(request, "fas")
    db: Database = request.app["db"]
    sid, acao = int(request.match_info["id"]), request.match_info["acao"]
    if acao == "cancelar":
        await db.conn.execute("UPDATE sorteios SET status = 'CANCELADO' WHERE id = ? AND status = 'ATIVO'", (sid,))
        await db.conn.commit()
        return _ok()
    ganhador = await extras.sortear(request.app["bot"], db, request.app["cfg"], sid)
    u = await db.get_usuario(ganhador) if ganhador else None
    return _ok(ganhador={"id": ganhador, "nome": (u or {}).get("nome") or "Fã", "username": (u or {}).get("username") or ""}
               if ganhador else None)


async def links_lista(request: web.Request) -> web.Response:
    _admin(request, "financeiro")
    db: Database = request.app["db"]
    bot = request.app["bot"]
    eu = await bot.me()
    itens = []
    for l in await db._todos("SELECT * FROM links ORDER BY ativo DESC, criado_em DESC"):
        entradas = (await db._um("SELECT COUNT(*) AS n FROM usuarios WHERE origem = ?", (l["codigo"],)))["n"]
        v = await db._um("SELECT COUNT(DISTINCT v.user_id) AS compradores, COALESCE(SUM(v.valor), 0) AS total FROM vendas v "
                         "JOIN usuarios u ON u.user_id = v.user_id WHERE u.origem = ? AND v.status = ? "
                         "AND COALESCE(v.metodo, 'pix') != 'saldo'", (l["codigo"], STATUS_PAGO))
        itens.append({"codigo": l["codigo"], "nome": l["nome"], "cupom": l["cupom"] or "", "ativo": bool(l["ativo"]),
                      "link": f"https://t.me/{eu.username}?start=l_{l['codigo']}", "entradas": entradas,
                      "compradores": v["compradores"], "total_fmt": fmt_reais(v["total"]),
                      "conversao": round(100 * v["compradores"] / entradas) if entradas else 0})
    return _ok(itens=itens)


async def link_novo(request: web.Request) -> web.Response:
    _admin(request, "financeiro")
    db: Database = request.app["db"]
    corpo = await _json(request)
    nome = str(corpo.get("nome", "")).strip()[:60]
    cupom = str(corpo.get("cupom", "")).strip().upper()[:30] or None
    if not nome:
        return _erro("Dê um nome ao link (ex: Instagram, TikTok, Bio).")
    if cupom and not await db.get_cupom(cupom):
        return _erro(f"O cupom {cupom} não existe. Crie em Ajustes › Cupons primeiro.")
    codigo = "".join(ch for ch in nome.upper() if ch.isalnum())[:12] or "LINK"
    if await db._um("SELECT 1 FROM links WHERE codigo = ?", (codigo,)):
        codigo = extras._codigo(codigo[:8], 4)
    await db.conn.execute("INSERT INTO links (codigo, nome, cupom, criado_em) VALUES (?, ?, ?, ?)", (codigo, nome, cupom, _agora()))
    await db.conn.commit()
    return _ok(codigo=codigo)


async def link_acao(request: web.Request) -> web.Response:
    _admin(request, "financeiro")
    db: Database = request.app["db"]
    codigo, acao = request.match_info["codigo"], request.match_info["acao"]
    await db.conn.execute("UPDATE links SET ativo = ? WHERE codigo = ?", (1 if acao == "ativar" else 0, codigo))
    await db.conn.commit()
    return _ok()


async def relatorio(request: web.Request) -> web.Response:
    user = _admin(request, "financeiro")
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    corpo = await _json(request)
    mes = str(corpo.get("mes") or datetime.now(extras._fuso(cfg)).strftime("%Y-%m"))
    try:
        datetime.strptime(mes, "%Y-%m")
    except ValueError:
        return _erro("Mês inválido.")
    try:
        resumo = await extras.enviar_relatorio(request.app["bot"], db, cfg, user["id"], mes)
    except Exception:
        log.exception("Relatório falhou")
        return _erro("Não consegui mandar o relatório no chat. Abra o bot e mande /start.", 502)
    return _ok(total_fmt=fmt_reais(resumo["total"]), vendas=resumo["vendas"])


async def vazamento(request: web.Request) -> web.Response:
    _admin(request, "fas")
    db: Database = request.app["db"]
    cfg = request.app["cfg"]
    from assinaturas import data_local
    uid = int(request.match_info["id"])
    u = await db.get_usuario(uid)
    if not u:
        return _erro("Ninguém com esse ID comprou ou falou com o bot.", 404)
    gasto = await db.total_gasto(uid)
    a = await db.get_assinatura(uid)
    compras = [p.titulo for p in await db.packs_do_cliente(uid)]
    return _ok(id=uid, nome=u.get("nome") or "", username=u.get("username") or "", bloqueado=bool(u.get("banido")),
               desde=data_local(u["criado_em"], cfg) if u.get("criado_em") else "", total=fmt_reais(gasto["total"]),
               compras=compras, vip=data_local(a.expira_em, cfg) if a and a.ativa else None,
               revogar=await R.valor(db, "vaz_revogar"))


async def vazamento_bloquear(request: web.Request) -> web.Response:
    _admin(request, "fas")
    db: Database = request.app["db"]
    import protecao
    uid = int(request.match_info["id"])
    erro = await protecao.bloquear(request.app["bot"], db, request.app["cfg"], uid)
    if erro:
        return _erro(erro)
    cortou = 0
    if await R.valor(db, "vaz_revogar"):
        cur = await db.conn.execute("DELETE FROM acessos WHERE user_id = ?", (uid,))
        await db.conn.execute("UPDATE saldos SET saldo = 0 WHERE user_id = ?", (uid,))
        await db.conn.commit()
        cortou = cur.rowcount
    return _ok(cortou=cortou)


async def backup_agora(request: web.Request) -> web.Response:
    user = _admin(request, "ajustes")
    import asyncio
    asyncio.create_task(extras.fazer_backup(request.app["bot"], request.app["db"], request.app["cfg"], chat_id=user["id"]))
    return _ok()


async def colecoes(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    db: Database = request.app["db"]
    rows = await db._todos("SELECT c.id, c.nome, (SELECT COUNT(*) FROM packs p WHERE p.colecao_id = c.id AND p.tipo = 'pack') AS n "
                           "FROM colecoes c ORDER BY c.ordem, c.id")
    return _ok(itens=[dict(r) for r in rows])


async def colecao_salvar(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    db: Database = request.app["db"]
    corpo = await _json(request)
    nome = str(corpo.get("nome", "")).strip()[:40]
    if corpo.get("apagar") and str(corpo.get("id", "")).isdigit():
        await db.conn.execute("UPDATE packs SET colecao_id = NULL WHERE colecao_id = ?", (int(corpo["id"]),))
        await db.conn.execute("DELETE FROM colecoes WHERE id = ?", (int(corpo["id"]),))
    elif not nome:
        return _erro("Dê um nome à coleção.")
    elif str(corpo.get("id", "")).isdigit():
        await db.conn.execute("UPDATE colecoes SET nome = ? WHERE id = ?", (nome, int(corpo["id"])))
    else:
        n = (await db._um("SELECT COUNT(*) AS n FROM colecoes"))["n"]
        if n >= 30:
            return _erro("Máximo de 30 coleções.")
        await db.conn.execute("INSERT INTO colecoes (nome, ordem) VALUES (?, ?)", (nome, n))
    await db.conn.commit()
    return await colecoes(request)


async def pack_colecao(request: web.Request) -> web.Response:
    _admin(request, "publicar")
    db: Database = request.app["db"]
    corpo = await _json(request)
    cid = corpo.get("colecao_id")
    cid = int(cid) if str(cid).isdigit() and await db._um("SELECT 1 FROM colecoes WHERE id = ?", (int(cid),)) else None
    await db.conn.execute("UPDATE packs SET colecao_id = ? WHERE id = ?", (cid, request.match_info["id"]))
    await db.conn.commit()
    return _ok()


async def relampago(request: web.Request) -> web.Response:
    _admin(request, "financeiro")
    db: Database = request.app["db"]
    pack = await db.get_pack(request.match_info["id"])
    if not pack or pack.tipo != "pack":
        return _erro("Pack não encontrado.", 404)
    if request.match_info["acao"] == "parar":
        await extras.parar_relampago(db, pack.id)
        return _ok()
    if not await R.valor(db, "relampago_ativo"):
        return _erro("Promoção relâmpago está desligada em Recursos.")
    corpo = await _json(request)
    try:
        pct = int(corpo.get("pct") or await R.valor(db, "relampago_pct"))
        horas = int(corpo.get("horas") or await R.valor(db, "relampago_horas"))
    except ValueError:
        return _erro("Valores inválidos.")
    if not 5 <= pct <= 90 or not 1 <= horas <= 72:
        return _erro("Use desconto de 5% a 90% e duração de 1 a 72 horas.")
    ate = await extras.iniciar_relampago(request.app["bot"], db, request.app["cfg"], pack, pct, horas)
    return _ok(ate=ate)


def registrar_rotas(app: web.Application) -> None:
    r = app.router
    r.add_post("/api/loja/teste_vip", teste_vip)
    r.add_get("/api/loja/carteira", carteira)
    r.add_post("/api/loja/pergunta", nova_pergunta)
    r.add_get("/api/loja/comentarios/{alvo}", comentarios)
    r.add_post("/api/loja/comentar", comentar)
    r.add_post("/api/loja/sorteio/{id:\\d+}/participar", participar)
    r.add_post("/api/loja/avaliar", avaliar)
    r.add_post("/api/loja/silenciar", silenciar)
    r.add_get("/api/loja/teaser/{pack_id}", teaser)
    r.add_get("/api/criadora/x/perguntas", perguntas_lista)
    r.add_post("/api/criadora/x/pergunta/{id:\\d+}/{acao:responder|recusar}", pergunta_acao)
    r.add_get("/api/criadora/x/comentarios", comentarios_pendentes)
    r.add_post("/api/criadora/x/comentario/{id:\\d+}/{acao:aprovar|recusar|apagar}", comentario_acao)
    r.add_get("/api/criadora/x/sorteios", sorteios_lista)
    r.add_post("/api/criadora/x/sorteio", sorteio_novo)
    r.add_post("/api/criadora/x/sorteio/{id:\\d+}/{acao:sortear|cancelar}", sorteio_acao)
    r.add_get("/api/criadora/x/links", links_lista)
    r.add_post("/api/criadora/x/link", link_novo)
    r.add_post("/api/criadora/x/link/{codigo}/{acao:ativar|desativar}", link_acao)
    r.add_post("/api/criadora/x/relatorio", relatorio)
    r.add_get("/api/criadora/x/vazamento/{id:\\d+}", vazamento)
    r.add_post("/api/criadora/x/vazamento/{id:\\d+}/bloquear", vazamento_bloquear)
    r.add_post("/api/criadora/x/backup", backup_agora)
    r.add_get("/api/criadora/x/colecoes", colecoes)
    r.add_post("/api/criadora/x/colecao", colecao_salvar)
    r.add_post("/api/criadora/x/pack/{id}/colecao", pack_colecao)
    r.add_post("/api/criadora/x/pack/{id}/relampago/{acao:iniciar|parar}", relampago)
