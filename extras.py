"""
Motor das melhorias opcionais (ligadas em Ajustes › Recursos, ver recursos.py).

  Vender mais     carteira de créditos, promoção relâmpago, upsell, teste grátis, oferta de volta,
                  renovação com PIX pronto, perguntas pagas, PPV pelo painel
  Engajar         níveis de fã, aviso de conteúdo novo, sequência de boas-vindas, aniversário, sorteios
  Divulgação      links rastreáveis (com cupom), indicação, relatório mensal
  Proteção        teaser em vídeo, backup das mídias

As rotas HTTP ficam em extras_rotas.py. Aqui ficam as regras, os itens pagáveis novos e as rotinas.
Itens pagáveis novos:
  credito_<centavos>   recarga da carteira (bônus calculado na entrega)
  pergunta_<id>        pergunta paga na caixinha
"""
import asyncio
import csv
import hashlib
import hmac
import html
import io
import logging
import os
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           Message, WebAppInfo)

import recursos as R
from config import Config
from database import ITENS_EXTRA, STATUS_PAGO, Database, Pack, Plano, _agora, agora, fmt_reais, iso

log = logging.getLogger(__name__)
GATEWAY = None                     # main.py coloca o gateway aqui (rotinas que geram PIX sozinhas)
INTERVALO = 300                    # rotinas a cada 5 minutos

SCHEMA = """
CREATE TABLE IF NOT EXISTS saldos (
    user_id     INTEGER PRIMARY KEY,
    saldo       INTEGER NOT NULL DEFAULT 0          -- centavos
);
CREATE TABLE IF NOT EXISTS movimentos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    valor       INTEGER NOT NULL,                   -- + crédito | - compra
    tipo        TEXT    NOT NULL,                   -- recarga | bonus | compra | indicacao | estorno
    ref         TEXT,
    criado_em   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS perguntas (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    texto        TEXT    NOT NULL,
    valor        INTEGER NOT NULL,
    onde         TEXT    NOT NULL DEFAULT 'feed',   -- feed | privado
    status       TEXT    NOT NULL DEFAULT 'AGUARDANDO',   -- AGUARDANDO (PIX) | NOVA | RESPONDIDA | RECUSADA
    resposta     TEXT,
    post_id      INTEGER,
    criado_em    TEXT    NOT NULL,
    pago_em      TEXT,
    respondida_em TEXT
);
CREATE TABLE IF NOT EXISTS comentarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alvo        TEXT    NOT NULL,                   -- post_N | pack_x | combo_x
    user_id     INTEGER NOT NULL,
    texto       TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pendente',   -- pendente | aprovado | recusado
    criado_em   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS sorteios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo      TEXT    NOT NULL,
    premio      TEXT    NOT NULL,
    publico     TEXT    NOT NULL DEFAULT 'vip',     -- vip | compradores | todos
    fim_em      TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'ATIVO',   -- ATIVO | SORTEADO | CANCELADO
    ganhador    INTEGER,
    criado_em   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS sorteio_part (
    sorteio_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (sorteio_id, user_id)
);
CREATE TABLE IF NOT EXISTS avaliacoes (
    user_id     INTEGER NOT NULL,
    pack_id     TEXT    NOT NULL,
    nota        INTEGER NOT NULL,
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (user_id, pack_id)
);
CREATE TABLE IF NOT EXISTS links (
    codigo      TEXT PRIMARY KEY,                   -- vai no link: t.me/bot?start=l_CODIGO
    nome        TEXT    NOT NULL,
    cupom       TEXT,                               -- aplicado sozinho nas compras de quem veio pelo link
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS colecoes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nome        TEXT    NOT NULL,
    ordem       INTEGER NOT NULL DEFAULT 0
);
-- Mensagens automáticas já enviadas (sequência, aniversário, oferta de volta, relatório...): nunca repete
CREATE TABLE IF NOT EXISTS marcos (
    user_id     INTEGER NOT NULL,
    tipo        TEXT    NOT NULL,
    ref         TEXT    NOT NULL,
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (user_id, tipo, ref)
);
CREATE TABLE IF NOT EXISTS backup_midias (
    file_id     TEXT PRIMARY KEY,
    chave       TEXT,                               -- nome do arquivo no destino
    tamanho     INTEGER,
    erro        TEXT,                               -- grande | falha
    criado_em   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coment_alvo ON comentarios(alvo, status);
CREATE INDEX IF NOT EXISTS idx_mov_user ON movimentos(user_id, criado_em);
"""


async def iniciar(db: Database) -> None:
    await db.conn.executescript(SCHEMA)
    await db.conn.commit()


def _codigo(prefixo: str, n: int = 6) -> str:
    """Código de cupom só com letras e números (o deep link do Telegram não aceita outros)."""
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return prefixo + "".join(secrets.choice(alfabeto) for _ in range(n))


async def marcar(db: Database, user_id: int, tipo: str, ref: str) -> bool:
    """True só na primeira vez (a rotina pode rodar de novo sem repetir a mensagem)."""
    cur = await db.conn.execute("INSERT OR IGNORE INTO marcos VALUES (?, ?, ?, ?)", (user_id, tipo, str(ref), _agora()))
    await db.conn.commit()
    return cur.rowcount == 1


def _fuso(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.fuso_horario)


def _local(dt_iso: str, cfg: Config, fmt: str = "%d/%m às %H:%M") -> str:
    return datetime.fromisoformat(dt_iso).astimezone(_fuso(cfg)).strftime(fmt)


async def _texto(db: Database, cfg: Config, modelo: str, user_id: int, **extra) -> str:
    u = await db.get_usuario(user_id) or {}
    vals = {"nome": html.escape((u.get("nome") or "").split(" ")[0] or "amor"), "criadora": html.escape(cfg.criadora_nome)}
    vals.update(extra)
    texto = html.escape(modelo)
    for k, v in vals.items():
        texto = texto.replace("{" + k + "}", str(v))
    return texto


def teclado_loja(cfg: Config, texto: str = "🛍️ Abrir a loja") -> list[list[InlineKeyboardButton]]:
    from loja import url_loja
    url = url_loja(cfg)
    return [[InlineKeyboardButton(text=texto, web_app=WebAppInfo(url=url))]] if url else []


# ════════════════════════════════════════════════════════════════════════════
#  Itens pagáveis novos
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class Recarga:
    preco: int
    sem_cupom = True

    @property
    def item_id(self) -> str:
        return f"credito_{self.preco}"

    @property
    def titulo(self) -> str:
        return f"Recarga da carteira · {fmt_reais(self.preco)}"

    @property
    def preco_fmt(self) -> str:
        return fmt_reais(self.preco)


@dataclass
class PerguntaPaga:
    id: int
    user_id: int
    texto: str
    preco: int
    status: str
    onde: str = "feed"
    sem_cupom = True
    dono_unico = True

    @property
    def item_id(self) -> str:
        return f"pergunta_{self.id}"

    @property
    def titulo(self) -> str:
        return f"Pergunta para a criadora #{self.id}"

    @property
    def preco_fmt(self) -> str:
        return fmt_reais(self.preco)

    @property
    def pagavel(self) -> bool:
        return self.status == "AGUARDANDO"


async def _item_recarga(db: Database, item_id: str):
    valor = item_id[8:]
    if not valor.isdigit() or not await R.valor(db, "carteira_ativo"):
        return None
    valor = int(valor)
    if valor not in [v * 100 for v in await R.lista(db, "carteira_valores")]:
        return None
    return Recarga(valor)


async def get_pergunta(db: Database, pergunta_id: int) -> PerguntaPaga | None:
    r = await db._um("SELECT * FROM perguntas WHERE id = ?", (pergunta_id,))
    return PerguntaPaga(r["id"], r["user_id"], r["texto"], r["valor"], r["status"], r["onde"]) if r else None


async def _item_pergunta(db: Database, item_id: str):
    return await get_pergunta(db, int(item_id[9:])) if item_id[9:].isdigit() else None


ITENS_EXTRA["credito_"] = _item_recarga
ITENS_EXTRA["pergunta_"] = _item_pergunta


# ════════════════════════════════════════════════════════════════════════════
#  Carteira
# ════════════════════════════════════════════════════════════════════════════
async def saldo(db: Database, user_id: int) -> int:
    r = await db._um("SELECT saldo FROM saldos WHERE user_id = ?", (user_id,))
    return r["saldo"] if r else 0


async def creditar(db: Database, user_id: int, valor: int, tipo: str, ref: str = "") -> int:
    await db.conn.execute("INSERT INTO saldos (user_id, saldo) VALUES (?, ?) "
                          "ON CONFLICT(user_id) DO UPDATE SET saldo = saldo + excluded.saldo", (user_id, valor))
    await db.conn.execute("INSERT INTO movimentos (user_id, valor, tipo, ref, criado_em) VALUES (?, ?, ?, ?, ?)",
                          (user_id, valor, tipo, ref, _agora()))
    await db.conn.commit()
    return await saldo(db, user_id)


async def debitar(db: Database, user_id: int, valor: int, ref: str) -> bool:
    """Atômico: só debita se tiver saldo suficiente."""
    cur = await db.conn.execute("UPDATE saldos SET saldo = saldo - ? WHERE user_id = ? AND saldo >= ?",
                                (valor, user_id, valor))
    if cur.rowcount != 1:
        await db.conn.commit()
        return False
    await db.conn.execute("INSERT INTO movimentos (user_id, valor, tipo, ref, criado_em) VALUES (?, ?, 'compra', ?, ?)",
                          (user_id, -valor, ref, _agora()))
    await db.conn.commit()
    return True


async def extrato(db: Database, user_id: int, limite: int = 20) -> list[dict]:
    rows = await db._todos("SELECT valor, tipo, ref, criado_em FROM movimentos WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                           (user_id, limite))
    return [dict(r) for r in rows]


async def bonus_recarga(db: Database, valor: int) -> int:
    pct = await R.valor(db, "carteira_bonus")
    return valor * pct // 100 if pct and valor >= await R.valor(db, "carteira_bonus_min") else 0


# ════════════════════════════════════════════════════════════════════════════
#  Preço: níveis, cupom do link de divulgação, desconto de indicado
# ════════════════════════════════════════════════════════════════════════════
async def nivel(db: Database, user_id: int) -> dict | None:
    if not await R.valor(db, "niveis_ativo"):
        return None
    total = (await db.total_gasto(user_id))["total"]
    c = await R.varios(db, "nivel_prata", "nivel_ouro", "desconto_prata", "desconto_ouro")
    if total >= c["nivel_ouro"]:
        return {"nivel": "ouro", "nome": "Ouro", "pct": c["desconto_ouro"], "total_fmt": fmt_reais(total), "proximo": None}
    if total >= c["nivel_prata"]:
        return {"nivel": "prata", "nome": "Prata", "pct": c["desconto_prata"], "total_fmt": fmt_reais(total),
                "proximo": {"nome": "Ouro", "falta_fmt": fmt_reais(c["nivel_ouro"] - total),
                            "pct": round(100 * (total - c["nivel_prata"]) / max(1, c["nivel_ouro"] - c["nivel_prata"]))}}
    return {"nivel": "bronze" if total else None, "nome": "Bronze" if total else "Sem nível", "pct": 0,
            "total_fmt": fmt_reais(total),
            "proximo": {"nome": "Prata", "falta_fmt": fmt_reais(c["nivel_prata"] - total),
                        "pct": round(100 * total / max(1, c["nivel_prata"]))}}


async def cupom_do_link(db: Database, user_id: int) -> str | None:
    u = await db._um("SELECT origem, origem_em FROM usuarios WHERE user_id = ?", (user_id,))
    if not u or not u["origem"] or not u["origem_em"]:
        return None
    link = await db._um("SELECT cupom FROM links WHERE codigo = ? AND ativo = 1", (u["origem"],))
    if not link or not link["cupom"]:
        return None
    dias = await R.valor(db, "links_cupom_dias")
    if datetime.fromisoformat(u["origem_em"]) + timedelta(days=dias) < agora():
        return None
    return link["cupom"]


async def desconto_automatico(db: Database, user_id: int, item) -> tuple[int, str] | None:
    """Maior desconto automático que vale para este fã neste item (sem cupom digitado): (pct, motivo)."""
    if not isinstance(item, Pack) and type(item).__name__ not in ("Combo", "Carrinho"):
        return None
    opcoes = []
    n = await nivel(db, user_id)
    if n and n["pct"]:
        opcoes.append((n["pct"], f"Desconto de fã {n['nome']} ({n['pct']}% OFF)"))
    if await R.valor(db, "indic_ativo"):
        pct = await R.valor(db, "indic_amigo_pct")
        u = await db._um("SELECT indicado_por FROM usuarios WHERE user_id = ?", (user_id,))
        if pct and u and u["indicado_por"] and not await db._um(
                "SELECT 1 FROM vendas WHERE user_id = ? AND status = ?", (user_id, STATUS_PAGO)):
            opcoes.append((pct, f"Presente de indicação ({pct}% OFF na 1ª compra)"))
    return max(opcoes) if opcoes else None


# ════════════════════════════════════════════════════════════════════════════
#  Entrega dos itens novos e o que acontece depois de qualquer pagamento
# ════════════════════════════════════════════════════════════════════════════
async def entregar_extra(bot: Bot, db: Database, cfg: Config, venda) -> bool:
    if venda.pack_id.startswith("credito_"):
        valor = int(venda.pack_id[8:])
        bonus = await bonus_recarga(db, valor)
        await creditar(db, venda.user_id, valor, "recarga", f"venda_{venda.id}")
        if bonus:
            await creditar(db, venda.user_id, bonus, "bonus", f"venda_{venda.id}")
        total = await saldo(db, venda.user_id)
        await bot.send_message(venda.user_id, f"💳 <b>Recarga confirmada!</b> {fmt_reais(valor)}"
                                              + (f" + bônus de {fmt_reais(bonus)}" if bonus else "")
                                              + f"\nSaldo da carteira: <b>{fmt_reais(total)}</b>.\n"
                                              "Agora é só comprar na loja com um toque.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=teclado_loja(cfg)) if teclado_loja(cfg) else None)
        return True
    if venda.pack_id.startswith("pergunta_"):
        p = await get_pergunta(db, int(venda.pack_id[9:]))
        if not p:
            return True
        cur = await db.conn.execute("UPDATE perguntas SET status = 'NOVA', pago_em = ? WHERE id = ? AND status = 'AGUARDANDO'",
                                    (_agora(), p.id))
        await db.conn.commit()
        if cur.rowcount != 1:
            await bot.send_message(venda.user_id, f"⚠️ Recebemos um segundo pagamento para a mesma pergunta. "
                                                  "A criadora foi avisada para devolver.")
            await _avisar_casa(bot, db, cfg, f"⚠️ A pergunta #{p.id} foi paga duas vezes. Devolva "
                                             f"{fmt_reais(venda.valor)} ao fã <code>{venda.user_id}</code>.")
            return True
        await bot.send_message(venda.user_id, f"✅ <b>Pergunta enviada!</b> {html.escape(cfg.criadora_nome)} vai responder "
                                              + ("no feed do VIP" if p.onde == "feed" else "aqui no privado") + ". Te aviso aqui.")
        await _avisar_casa(bot, db, cfg, f"❓ <b>Pergunta nova · {fmt_reais(p.preco)}</b>\n{html.escape(p.texto)}\n\n"
                                         "Responda pelo Painel da loja.", "pedidos")
        return True
    return False


async def _avisar_casa(bot: Bot, db: Database, cfg: Config, texto: str, perm: str | None = None) -> None:
    """Avisa a criadora (e a equipe que tem a permissão `perm`)."""
    import contas
    destinos = set(cfg.admin_ids) | ({tid for tid, p in contas.EQUIPE.items() if perm in p} if perm else set())
    for uid in destinos:
        try:
            await bot.send_message(uid, texto)
        except Exception:
            pass


async def pos_pagamento(bot: Bot, db: Database, cfg: Config, venda) -> None:
    await _indicacao(bot, db, cfg, venda)
    if venda.pack_id.startswith(("pack_", "combo_")):
        await oferecer_upsell(bot, db, cfg, venda.user_id)


async def _indicacao(bot: Bot, db: Database, cfg: Config, venda) -> None:
    if not await R.valor(db, "indic_ativo"):
        return
    u = await db._um("SELECT indicado_por, indicacao_paga, nome FROM usuarios WHERE user_id = ?", (venda.user_id,))
    if not u or not u["indicado_por"] or u["indicacao_paga"]:
        return
    if await R.valor(db, "indic_quando") == "vip" and not venda.pack_id.startswith("plano_"):
        return
    cur = await db.conn.execute("UPDATE usuarios SET indicacao_paga = 1 WHERE user_id = ? AND indicacao_paga = 0",
                                (venda.user_id,))
    await db.conn.commit()
    if cur.rowcount != 1:
        return
    quem, valor = u["indicado_por"], await R.valor(db, "indic_recompensa")
    if await db.esta_banido(quem):
        return
    amigo = html.escape((u["nome"] or "Seu amigo").split(" ")[0])
    try:
        if await R.valor(db, "carteira_ativo"):
            total = await creditar(db, quem, valor, "indicacao", str(venda.user_id))
            await bot.send_message(quem, f"🎁 <b>{amigo} entrou pela sua indicação!</b>\nGanhou {fmt_reais(valor)} na carteira. "
                                         f"Saldo: <b>{fmt_reais(total)}</b>.",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=teclado_loja(cfg)) or None)
        else:
            codigo = _codigo("IND")
            await db.criar_cupom(codigo, "fixo", valor, user_id=quem, usos_max=1, expira_em=agora() + timedelta(days=30))
            await bot.send_message(quem, f"🎁 <b>{amigo} entrou pela sua indicação!</b>\nSeu presente: cupom "
                                         f"<code>{codigo}</code> de {fmt_reais(valor)} OFF (vale 30 dias).")
    except Exception:
        log.warning("Não consegui avisar quem indicou (%s)", quem)


async def oferecer_upsell(bot: Bot, db: Database, cfg: Config, user_id: int) -> dict | None:
    """Depois de comprar, um outro pack com desconto por pouco tempo (cupom pessoal de uso único)."""
    if not await R.valor(db, "upsell_ativo"):
        return None
    atual = await upsell_ativo(db, user_id)
    if atual:
        return atual
    curtidas = await db.curtidas_do_feed(0)
    candidatos = [p for p in await db.listar_packs() if not await db.ja_comprou(user_id, p.id)]
    if not candidatos:
        return None
    pack = max(candidatos, key=lambda p: (curtidas.get(p.id, {}).get("n", 0), p.criado_em))
    pct, minutos = await R.valor(db, "upsell_pct"), await R.valor(db, "upsell_minutos")
    codigo = _codigo("UP")
    await db.criar_cupom(codigo, "pct", pct, item_id=pack.id, user_id=user_id, usos_max=1,
                         expira_em=agora() + timedelta(minutes=minutos))
    preco = pack.preco_atual - pack.preco_atual * pct // 100
    teclado = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🔓 Quero · {fmt_reais(max(100, preco))}", callback_data=f"comprar:{pack.id}_{codigo}")]])
    texto = (f"✨ <b>Leve também: {html.escape(pack.titulo)}</b>\nSó para você, {pct}% OFF: de <s>{pack.preco_fmt}</s> por "
             f"<b>{fmt_reais(max(100, preco))}</b>. Vale por {minutos} minutos.")
    try:
        if pack.blur:
            await bot.send_photo(user_id, pack.blur[0]["file_id"], caption=texto, reply_markup=teclado)
        else:
            await bot.send_message(user_id, texto, reply_markup=teclado)
    except Exception:
        pass
    return await upsell_ativo(db, user_id)


async def upsell_ativo(db: Database, user_id: int) -> dict | None:
    r = await db._um("SELECT codigo, valor, item_id, expira_em FROM cupons WHERE user_id = ? AND codigo LIKE 'UP%' "
                     "AND ativo = 1 AND usos < COALESCE(usos_max, 1) AND expira_em > ? ORDER BY criado_em DESC LIMIT 1",
                     (user_id, _agora()))
    if not r:
        return None
    pack = await db.get_pack(r["item_id"])
    if not pack or pack.tipo != "pack" or await db.ja_comprou(user_id, pack.id):
        return None
    preco = max(100, pack.preco_atual - pack.preco_atual * r["valor"] // 100)
    return {"pack_id": pack.id, "titulo": pack.titulo, "cupom": r["codigo"], "pct": r["valor"],
            "preco_fmt": fmt_reais(preco), "de_fmt": fmt_reais(pack.preco_atual), "ate": r["expira_em"]}


# ════════════════════════════════════════════════════════════════════════════
#  Teste grátis do VIP
# ════════════════════════════════════════════════════════════════════════════
async def teste_disponivel(db: Database, user_id: int) -> int:
    """Dias de teste que este fã pode pegar (0 = não pode)."""
    if not await R.valor(db, "teste_ativo") or await db.ja_assinou(user_id) or await db.esta_banido(user_id):
        return 0
    return await R.valor(db, "teste_dias")


async def ativar_teste(bot: Bot, db: Database, cfg: Config, user_id: int) -> str | None:
    dias = await teste_disponivel(db, user_id)
    if not dias:
        return "O teste grátis é só para quem nunca assinou."
    from assinaturas import ativar_assinatura
    from types import SimpleNamespace
    plano = Plano(None, f"Teste grátis de {dias} dias", dias, 0)
    await ativar_assinatura(bot, db, cfg, SimpleNamespace(user_id=user_id), plano)
    await marcar(db, user_id, "teste", "1")
    await db.marcar_aviso_assinatura(user_id, 1)      # teste é curto: pula o aviso de "vence em 3 dias"
    await _avisar_casa(bot, db, cfg, f"🎁 Um fã começou o teste grátis do VIP ({dias} dias) · <code>{user_id}</code>")
    return None


# ════════════════════════════════════════════════════════════════════════════
#  Aviso de conteúdo novo e promoção relâmpago
# ════════════════════════════════════════════════════════════════════════════
async def _publico(db: Database, cfg: Config, publico: str) -> list[int]:
    if publico == "todos":
        ids = await db.todos_clientes()
    elif publico == "compradores":
        ids = await db.compradores()
    else:
        ids = await db.assinantes_ativos()
    calados = {r["user_id"] for r in await db._todos("SELECT user_id FROM usuarios WHERE silenciado = 1")}
    return [u for u in ids if u not in calados and u not in cfg.admin_ids]


def _teclado_novidade(cfg: Config, botao: InlineKeyboardButton | None) -> InlineKeyboardMarkup:
    linhas = [[botao]] if botao else teclado_loja(cfg, "🛍️ Ver na loja")
    linhas.append([InlineKeyboardButton(text="🔕 Não avisar mais", callback_data="silenciar")])
    return InlineKeyboardMarkup(inline_keyboard=linhas)


async def novo_conteudo(bot: Bot, db: Database, cfg: Config, pack: Pack | None = None, post: dict | None = None) -> None:
    """Chamado quando um pack ou post vai ao ar."""
    try:
        if not await R.valor(db, "novidades_ativo") or (post and not await R.valor(db, "novidades_posts")):
            return
        destinos = await _publico(db, cfg, await R.valor(db, "novidades_publico"))
        if not destinos:
            return
        if pack:
            texto = f"🔥 <b>Saiu pack novo: {html.escape(pack.titulo)}</b>\n{html.escape((pack.legenda or '')[:160])}"
            botao = InlineKeyboardButton(text=f"🔓 Desbloquear · {fmt_reais(pack.preco_atual)}", callback_data=f"comprar:{pack.id}")
            capa = pack.blur[0]["file_id"] if pack.blur else None
        else:
            texto = f"✨ <b>Post novo de {html.escape(cfg.criadora_nome)}</b>\n{html.escape((post.get('texto') or '')[:200])}"
            botao, capa = None, None
        teclado = _teclado_novidade(cfg, botao)

        async def enviar(uid: int):
            if capa:
                await bot.send_photo(uid, capa, caption=texto, reply_markup=teclado)
            else:
                await bot.send_message(uid, texto, reply_markup=teclado)

        from envio import enviar_em_massa
        asyncio.create_task(enviar_em_massa(db, cfg, destinos, enviar))
    except Exception:
        log.exception("Aviso de conteúdo novo falhou")


async def enviar_ppv(bot: Bot, db: Database, cfg: Config, pack: Pack, publico: str, avisar_chat: int | None = None) -> int:
    """Mensagem paga no privado (PPV): prévia embaçada + botão de desbloquear. publico: vip | todos | compradores | fa:<id>."""
    if publico.startswith("fa:") and publico[3:].isdigit():
        destinos = [int(publico[3:])]
    elif publico == "todos":
        destinos = await db.todos_clientes()
    elif publico == "compradores":
        destinos = await db.compradores()
    else:
        destinos = await db.assinantes_ativos()
    destinos = [u for u in destinos if u not in cfg.admin_ids]
    teclado = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔓 Desbloquear · {fmt_reais(pack.preco_atual)}", callback_data=f"comprar:{pack.id}")]])
    legenda = (f"💌 <b>{html.escape(pack.titulo)}</b>\n" + (html.escape(pack.legenda[:300]) + "\n" if pack.legenda else "")
               + f"\nMensagem exclusiva. Desbloqueie por <b>{fmt_reais(pack.preco_atual)}</b> 👇")

    async def enviar(uid: int):
        if pack.blur:
            await bot.send_photo(uid, pack.blur[0]["file_id"], caption=legenda, reply_markup=teclado)
        else:
            await bot.send_message(uid, legenda, reply_markup=teclado)

    async def tarefa():
        from envio import enviar_em_massa, resumo_envio
        res = await enviar_em_massa(db, cfg, destinos, enviar)
        if avisar_chat:
            try:
                await bot.send_message(avisar_chat, f"✅ Mensagem paga “{html.escape(pack.titulo)}” enviada.\n" + resumo_envio(res))
            except Exception:
                pass
    asyncio.create_task(tarefa())
    return len(destinos)


async def iniciar_relampago(bot: Bot, db: Database, cfg: Config, pack: Pack, pct: int, horas: int) -> str:
    ate = agora() + timedelta(hours=horas)
    await db.conn.execute("UPDATE packs SET promo_pct = ?, promo_ate = ? WHERE id = ?", (pct, iso(ate), pack.id))
    await db.conn.commit()
    if await R.valor(db, "relampago_avisar"):
        pack = await db.get_pack(pack.id)
        destinos = await _publico(db, cfg, "vip")
        texto = (f"⚡ <b>Promoção relâmpago!</b>\n{html.escape(pack.titulo)} com <b>{pct}% OFF</b>: de <s>{pack.preco_fmt}</s> "
                 f"por <b>{fmt_reais(pack.preco_atual)}</b>.\nSó até {_local(iso(ate), cfg, '%H:%M')}"
                 + (f" de {_local(iso(ate), cfg, '%d/%m')}" if horas > 12 else "") + ".")
        teclado = _teclado_novidade(cfg, InlineKeyboardButton(text=f"⚡ Aproveitar · {fmt_reais(pack.preco_atual)}",
                                                              callback_data=f"comprar:{pack.id}"))
        capa = pack.blur[0]["file_id"] if pack.blur else None

        async def enviar(uid: int):
            if capa:
                await bot.send_photo(uid, capa, caption=texto, reply_markup=teclado)
            else:
                await bot.send_message(uid, texto, reply_markup=teclado)

        from envio import enviar_em_massa
        asyncio.create_task(enviar_em_massa(db, cfg, destinos, enviar))
    return iso(ate)


async def parar_relampago(db: Database, pack_id: str) -> None:
    await db.conn.execute("UPDATE packs SET promo_pct = 0, promo_ate = NULL WHERE id = ?", (pack_id,))
    await db.conn.commit()


# ════════════════════════════════════════════════════════════════════════════
#  PIX enviado pelo bot sem o fã pedir (renovação com PIX pronto)
# ════════════════════════════════════════════════════════════════════════════
async def enviar_pix(bot: Bot, db: Database, cfg: Config, user_id: int, item_id: str, cupom: str | None = None,
                     cabecalho: str = "") -> bool:
    from vendas import criar_pedido
    if GATEWAY is None:
        return False
    pedido = await criar_pedido(db, GATEWAY, cfg, user_id, item_id, cupom)
    if pedido.status != "ok":
        return False
    c = pedido.cobranca
    hora = pedido.expira.astimezone(_fuso(cfg)).strftime("%H:%M")
    legenda = (f"{cabecalho}🔥 <b>{html.escape(pedido.item.titulo)}</b>\n💰 <b>{fmt_reais(pedido.preco)}</b>"
               + (f" ({html.escape(pedido.promo)})" if pedido.promo else "")
               + f"\n\n📲 PIX Copia e Cola (toque para copiar):\n<code>{c.copia_e_cola}</code>\n\n⌛ Válido até {hora}.")
    teclado = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Já paguei", callback_data=f"pix:{c.pix_id}")]])
    await bot.send_photo(user_id, BufferedInputFile(c.qr_png, filename="pix.png"), caption=legenda, reply_markup=teclado)
    return True


# ════════════════════════════════════════════════════════════════════════════
#  Rotinas (a cada 5 minutos)
# ════════════════════════════════════════════════════════════════════════════
async def rotina_volta(bot: Bot, db: Database, cfg: Config) -> int:
    if not await R.valor(db, "volta_ativo"):
        return 0
    c = await R.varios(db, "volta_dias", "volta_pct", "volta_horas", "volta_msg")
    fim = agora() - timedelta(days=c["volta_dias"])
    rows = await db._todos("SELECT user_id, plano_id, expira_em FROM assinaturas WHERE status = 'VENCIDA' "
                           "AND expira_em <= ? AND expira_em > ?", (iso(fim), iso(fim - timedelta(days=3))))
    from assinaturas import teclado_planos
    planos = await db.listar_planos()
    feitos = 0
    for r in rows:
        if await db.assinante_ativo(r["user_id"]) or await db.esta_banido(r["user_id"]):
            continue
        if not await marcar(db, r["user_id"], "volta", r["expira_em"][:16]):
            continue
        codigo = _codigo("VOLTA")
        validade = agora() + timedelta(hours=c["volta_horas"])
        await db.criar_cupom(codigo, "pct", c["volta_pct"], user_id=r["user_id"], usos_max=1, expira_em=validade)
        teclado = teclado_planos(planos)
        for linha in teclado.inline_keyboard:              # os botões já levam o cupom
            linha[0].callback_data = f"{linha[0].callback_data}_{codigo}"
        texto = await _texto(db, cfg, c["volta_msg"], r["user_id"], desconto=f"{c['volta_pct']}% OFF",
                             validade=_local(iso(validade), cfg), cupom=codigo)
        try:
            await bot.send_message(r["user_id"], texto + f"\n\n🏷️ Cupom: <code>{codigo}</code>", reply_markup=teclado)
            feitos += 1
        except Exception:
            pass
    return feitos


SEQ = [("seq_dia1", 0, 2), ("seq_dia3", 2, 4), ("seq_dia7", 6, 9)]


async def rotina_sequencia(bot: Bot, db: Database, cfg: Config) -> int:
    if not await R.valor(db, "seq_ativo"):
        return 0
    rows = await db._todos("SELECT user_id, inicio_em FROM assinaturas WHERE status = 'ATIVA' AND expira_em > ? "
                           "AND inicio_em >= ?", (_agora(), iso(agora() - timedelta(days=10))))
    feitos = 0
    for r in rows:
        dias = (agora() - datetime.fromisoformat(r["inicio_em"])).total_seconds() / 86400
        for chave, de, ate in SEQ:
            if not de <= dias < ate:
                continue
            modelo = (await R.valor(db, chave) or "").strip()
            if not modelo or not await marcar(db, r["user_id"], "seq", chave):
                continue
            try:
                await bot.send_message(r["user_id"], await _texto(db, cfg, modelo, r["user_id"]),
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=teclado_loja(cfg)) or None)
                feitos += 1
            except Exception:
                pass
    return feitos


async def rotina_aniversario(bot: Bot, db: Database, cfg: Config) -> int:
    if not await R.valor(db, "aniv_ativo"):
        return 0
    meses_alvo = await R.lista(db, "aniv_meses")
    c = await R.varios(db, "aniv_pct", "aniv_msg")
    rows = await db._todos("SELECT user_id, inicio_em FROM assinaturas WHERE status = 'ATIVA' AND expira_em > ?", (_agora(),))
    feitos = 0
    for r in rows:
        meses = int((agora() - datetime.fromisoformat(r["inicio_em"])).days // 30)
        if meses not in meses_alvo or not await marcar(db, r["user_id"], "aniv", str(meses)):
            continue
        desconto, extra = "", ""
        if c["aniv_pct"]:
            codigo = _codigo("ANIV")
            await db.criar_cupom(codigo, "pct", c["aniv_pct"], user_id=r["user_id"], usos_max=1,
                                 expira_em=agora() + timedelta(days=7))
            desconto, extra = f"{c['aniv_pct']}% OFF", f"\n\n🏷️ Cupom: <code>{codigo}</code> (vale 7 dias)"
        rotulo = f"{meses} {'mês' if meses == 1 else 'meses'}"
        try:
            await bot.send_message(r["user_id"], await _texto(db, cfg, c["aniv_msg"], r["user_id"], meses=rotulo,
                                                              desconto=desconto or "um carinho") + extra,
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=teclado_loja(cfg)) or None)
            feitos += 1
        except Exception:
            pass
    return feitos


async def pode_participar(db: Database, cfg: Config, sorteio: dict, user_id: int) -> bool:
    if user_id in cfg.admin_ids or await db.esta_banido(user_id):
        return False
    if sorteio["publico"] == "vip":
        return await db.assinante_ativo(user_id)
    if sorteio["publico"] == "compradores":
        return await db._um("SELECT 1 FROM vendas WHERE user_id = ? AND status = ?", (user_id, STATUS_PAGO)) is not None
    return True


async def sortear(bot: Bot, db: Database, cfg: Config, sorteio_id: int) -> int | None:
    s = await db._um("SELECT * FROM sorteios WHERE id = ? AND status = 'ATIVO'", (sorteio_id,))
    if not s:
        return None
    s = dict(s)
    parts = [r["user_id"] for r in await db._todos("SELECT user_id FROM sorteio_part WHERE sorteio_id = ?", (sorteio_id,))]
    validos = [u for u in parts if await pode_participar(db, cfg, s, u)]
    ganhador = secrets.choice(validos) if validos else None
    cur = await db.conn.execute("UPDATE sorteios SET status = 'SORTEADO', ganhador = ? WHERE id = ? AND status = 'ATIVO'",
                                (ganhador, sorteio_id))
    await db.conn.commit()
    if cur.rowcount != 1:
        return None
    if ganhador:
        u = await db.get_usuario(ganhador) or {}
        try:
            await bot.send_message(ganhador, f"🎉 <b>Você ganhou o sorteio “{html.escape(s['titulo'])}”!</b>\n"
                                             f"Prêmio: {html.escape(s['premio'])}\n{html.escape(cfg.criadora_nome)} vai falar com você.")
        except Exception:
            pass
        await _avisar_casa(bot, db, cfg, f"🎲 <b>Sorteio “{html.escape(s['titulo'])}”</b>\nGanhador(a): "
                                         f"{html.escape(u.get('nome') or 'Fã')}"
                                         + (f" (@{html.escape(u['username'])})" if u.get("username") else "")
                                         + f" · <code>{ganhador}</code>\n{len(validos)} participante(s). Combine a entrega do prêmio.")
    else:
        await _avisar_casa(bot, db, cfg, f"🎲 O sorteio “{html.escape(s['titulo'])}” terminou sem participantes.")
    return ganhador


async def rotina_sorteios(bot: Bot, db: Database, cfg: Config) -> int:
    rows = await db._todos("SELECT id FROM sorteios WHERE status = 'ATIVO' AND fim_em <= ?", (_agora(),))
    for r in rows:
        await sortear(bot, db, cfg, r["id"])
    return len(rows)


async def rotina_relatorio(bot: Bot, db: Database, cfg: Config) -> int:
    if not await R.valor(db, "relat_auto"):
        return 0
    agora_local = datetime.now(_fuso(cfg))
    if agora_local.day != 1 or agora_local.hour < 9:
        return 0
    mes_passado = (agora_local.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if not await marcar(db, 0, "relatorio", mes_passado):
        return 0
    for uid in cfg.admin_ids:
        try:
            await enviar_relatorio(bot, db, cfg, uid, mes_passado)
        except Exception:
            log.exception("Relatório mensal falhou para %s", uid)
    return 1


async def rotina_backup(bot: Bot, db: Database, cfg: Config) -> int:
    if not await R.valor(db, "bkm_ativo"):
        return 0
    agora_local = datetime.now(_fuso(cfg))
    if agora_local.hour < 5 or not await marcar(db, 0, "backup_midias", agora_local.strftime("%Y-%m-%d")):
        return 0
    asyncio.create_task(fazer_backup(bot, db, cfg, avisar=False))
    return 1


async def rodar_rotinas(bot: Bot, db: Database, cfg: Config) -> None:
    for rotina in (rotina_sorteios, rotina_volta, rotina_sequencia, rotina_aniversario, rotina_relatorio, rotina_backup):
        try:
            n = await rotina(bot, db, cfg)
            if n:
                log.info("Rotina %s: %d", rotina.__name__, n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Rotina %s falhou", rotina.__name__)


async def loop_extras(bot: Bot, db: Database, cfg: Config) -> None:
    await asyncio.sleep(15)
    while True:
        await rodar_rotinas(bot, db, cfg)
        await asyncio.sleep(INTERVALO)


# ════════════════════════════════════════════════════════════════════════════
#  Relatório mensal (CSV)
# ════════════════════════════════════════════════════════════════════════════
def _tipo_venda(item_id: str) -> str:
    for prefixo, nome in (("plano_", "Assinatura VIP"), ("gorjeta_", "Mimo"), ("credito_", "Recarga da carteira"),
                          ("pergunta_", "Pergunta paga"), ("pedido_", "Pedido personalizado"), ("carrinho_", "Carrinho"),
                          ("combo_", "Combo")):
        if item_id.startswith(prefixo):
            return nome
    return "Pack"


async def gerar_relatorio(db: Database, cfg: Config, mes: str) -> tuple[bytes, dict]:
    """CSV das vendas pagas do mês (AAAA-MM, no fuso da criadora) e um resumo."""
    fuso = _fuso(cfg)
    ini = datetime.strptime(mes + "-01", "%Y-%m-%d").replace(tzinfo=fuso)
    fim = (ini + timedelta(days=32)).replace(day=1)
    rows = await db._todos(
        "SELECT v.*, u.nome, u.username FROM vendas v LEFT JOIN usuarios u ON u.user_id = v.user_id "
        "WHERE v.status = ? AND v.pago_em >= ? AND v.pago_em < ? ORDER BY v.pago_em",
        (STATUS_PAGO, iso(ini.astimezone(timezone.utc)), iso(fim.astimezone(timezone.utc))))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Data", "Hora", "Tipo", "Item", "Valor (R$)", "Preço cheio (R$)", "Cupom", "Pagamento", "ID do fã", "Nome", "Usuário"])
    resumo = {"vendas": 0, "total": 0, "por_tipo": {}}
    titulos: dict[str, str] = {}
    for r in rows:
        quando = datetime.fromisoformat(r["pago_em"]).astimezone(fuso)
        if r["pack_id"] not in titulos:
            item = await db.get_item(r["pack_id"])
            titulos[r["pack_id"]] = getattr(item, "titulo", r["pack_id"]) if item else r["pack_id"]
        metodo = r["metodo"] if "metodo" in r.keys() and r["metodo"] else "pix"
        tipo = _tipo_venda(r["pack_id"])
        w.writerow([quando.strftime("%d/%m/%Y"), quando.strftime("%H:%M"), tipo, titulos[r["pack_id"]],
                    f"{r['valor'] / 100:.2f}".replace(".", ","),
                    f"{(r['valor_original'] or r['valor']) / 100:.2f}".replace(".", ","),
                    r["cupom"] or "", "Saldo da carteira" if metodo == "saldo" else "PIX", r["user_id"],
                    r["nome"] or "", ("@" + r["username"]) if r["username"] else ""])
        if metodo != "saldo":                    # o dinheiro entrou na recarga; não conta duas vezes
            resumo["vendas"] += 1
            resumo["total"] += r["valor"]
            resumo["por_tipo"][tipo] = resumo["por_tipo"].get(tipo, 0) + r["valor"]
    return buf.getvalue().encode("utf-8-sig"), resumo     # BOM: o Excel abre com acentos certos


async def enviar_relatorio(bot: Bot, db: Database, cfg: Config, chat_id: int, mes: str) -> dict:
    dados, resumo = await gerar_relatorio(db, cfg, mes)
    nome_mes = datetime.strptime(mes, "%Y-%m").strftime("%m/%Y")
    linhas = [f"📊 <b>Relatório de {nome_mes}</b>", f"Recebido: <b>{fmt_reais(resumo['total'])}</b> em {resumo['vendas']} pagamento(s)"]
    linhas += [f"• {t}: {fmt_reais(v)}" for t, v in sorted(resumo["por_tipo"].items(), key=lambda x: -x[1])]
    linhas.append("\nA planilha abre no Excel ou no Google Planilhas. Compras pagas com saldo aparecem, "
                  "mas não somam de novo (o dinheiro entrou na recarga).")
    await bot.send_document(chat_id, BufferedInputFile(dados, filename=f"relatorio-{mes}.csv"), caption="\n".join(linhas))
    return resumo


# ════════════════════════════════════════════════════════════════════════════
#  Teaser em vídeo (prévia curta borrada, gerada sob demanda e guardada em disco)
# ════════════════════════════════════════════════════════════════════════════
DIR_TEASERS = Path(tempfile.gettempdir()) / "teasers"
_vez_teaser: asyncio.Semaphore | None = None
_gerando_teaser: dict[str, asyncio.Task] = {}


def _gerar_teaser(dados: bytes, destino: Path, segundos: int, blur: int) -> bool:
    DIR_TEASERS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        entrada, saida = Path(tmp, "in.mp4"), Path(tmp, "out.mp4")
        entrada.write_bytes(dados)
        for inicio in ("1", "0"):
            proc = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-threads", "1", "-ss", inicio, "-t", str(segundos), "-i", str(entrada),
                 "-vf", f"scale=360:-2,boxblur={blur}:2,format=yuv420p", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                 "-crf", "32", "-threads", "1", "-movflags", "+faststart", str(saida)],
                capture_output=True, timeout=180)
            if proc.returncode == 0 and saida.exists() and saida.stat().st_size > 0:
                os.replace(saida, destino)
                return True
        log.warning("Teaser: ffmpeg falhou (%s): %s", proc.returncode, proc.stderr[-300:])
    return False


async def teaser(bot: Bot, db: Database, pack: Pack) -> Path | None:
    global _vez_teaser
    if not await R.valor(db, "teaser_ativo"):
        return None
    video = next((m for m in pack.originais if m["tipo"] == "video"), None)
    if not video:
        return None
    seg, blur = await R.valor(db, "teaser_segundos"), await R.valor(db, "teaser_blur")
    nome = hashlib.sha256(f"{video['file_id']}:{seg}:{blur}".encode()).hexdigest()[:32]
    destino = DIR_TEASERS / f"{nome}.mp4"
    if destino.exists():
        return destino
    tarefa = _gerando_teaser.get(nome)
    if tarefa is None:
        async def gerar():
            global _vez_teaser
            if _vez_teaser is None:
                _vez_teaser = asyncio.Semaphore(1)
            try:
                async with _vez_teaser:
                    if destino.exists():
                        return destino
                    import galeria
                    arq = await bot.get_file(video["file_id"])
                    if (arq.file_size or 0) > galeria.LIMITE_BOT_API:
                        return None
                    dados = await galeria._baixar(bot, video["file_id"])
                    return destino if await asyncio.to_thread(_gerar_teaser, dados, destino, seg, blur) else None
            except Exception:
                log.exception("Teaser do pack %s falhou", pack.id)
                return None
            finally:
                _gerando_teaser.pop(nome, None)
        tarefa = _gerando_teaser[nome] = asyncio.create_task(gerar())
    return await asyncio.shield(tarefa)


# ════════════════════════════════════════════════════════════════════════════
#  Backup das mídias (disco do servidor ou bucket S3)
# ════════════════════════════════════════════════════════════════════════════
_backup_rodando = False


def _assinar_s3(chave: bytes, msg: str) -> bytes:
    return hmac.new(chave, msg.encode(), hashlib.sha256).digest()


async def _s3_put(conf: dict, nome: str, dados: bytes, tipo: str) -> None:
    """PUT com assinatura AWS SigV4 (funciona com Railway Buckets, Cloudflare R2, AWS S3, MinIO...)."""
    import httpx
    from urllib.parse import quote, urlparse
    endpoint = conf["bkm_s3_endpoint"].rstrip("/")
    host = urlparse(endpoint).netloc
    caminho = f"/{conf['bkm_s3_bucket']}/{quote(nome)}"
    agora_ = datetime.now(timezone.utc)
    amz_data, data = agora_.strftime("%Y%m%dT%H%M%SZ"), agora_.strftime("%Y%m%d")
    regiao = conf["bkm_s3_regiao"] or "auto"
    hash_corpo = hashlib.sha256(dados).hexdigest()
    cab_assinados = "host;x-amz-content-sha256;x-amz-date"
    canonico = f"PUT\n{caminho}\n\nhost:{host}\nx-amz-content-sha256:{hash_corpo}\nx-amz-date:{amz_data}\n\n{cab_assinados}\n{hash_corpo}"
    escopo = f"{data}/{regiao}/s3/aws4_request"
    texto = f"AWS4-HMAC-SHA256\n{amz_data}\n{escopo}\n{hashlib.sha256(canonico.encode()).hexdigest()}"
    k = _assinar_s3(("AWS4" + conf["bkm_s3_segredo"]).encode(), data)
    for parte in (regiao, "s3", "aws4_request"):
        k = _assinar_s3(k, parte)
    assinatura = hmac.new(k, texto.encode(), hashlib.sha256).hexdigest()
    cab = {"x-amz-content-sha256": hash_corpo, "x-amz-date": amz_data, "Content-Type": tipo,
           "Authorization": f"AWS4-HMAC-SHA256 Credential={conf['bkm_s3_chave']}/{escopo}, "
                            f"SignedHeaders={cab_assinados}, Signature={assinatura}"}
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.put(endpoint + caminho, content=dados, headers=cab)
        if r.status_code >= 300:
            raise RuntimeError(f"S3 respondeu {r.status_code}: {r.text[:200]}")


async def _todas_midias(db: Database) -> list[tuple[str, str]]:
    """(file_id, tipo) de tudo que o bot guarda: packs (inclusive apagados), posts, stories, foto de perfil."""
    import json
    vistos, saida = set(), []

    def add(fid, tipo):
        if fid and fid not in vistos:
            vistos.add(fid)
            saida.append((fid, tipo))

    for r in await db._todos("SELECT file_ids_originais, file_ids_blur FROM packs"):
        for m in json.loads(r["file_ids_originais"]) + json.loads(r["file_ids_blur"]):
            add(m["file_id"], m["tipo"])
    for r in await db._todos("SELECT foto, video FROM posts"):
        add(r["foto"], "photo")
        add(r["video"], "video")
    for r in await db._todos("SELECT tipo, file_id, capa FROM stories"):
        add(r["file_id"], r["tipo"])
        add(r["capa"], "photo")
    for r in await db._todos("SELECT valor FROM ajustes WHERE chave = 'foto_perfil' OR chave LIKE 'msg_%_foto'"):
        add(r["valor"], "photo")
    return saida


async def fazer_backup(bot: Bot, db: Database, cfg: Config, avisar: bool = True, chat_id: int | None = None) -> dict:
    global _backup_rodando
    if _backup_rodando:
        return {"rodando": True}
    _backup_rodando = True
    res = {"novos": 0, "grandes": 0, "falhas": 0, "total": 0}
    try:
        import galeria
        conf = await R.varios(db, "bkm_destino", "bkm_s3_endpoint", "bkm_s3_bucket", "bkm_s3_regiao", "bkm_s3_chave", "bkm_s3_segredo")
        s3 = conf["bkm_destino"] == "s3"
        if s3 and not all(conf[k] for k in ("bkm_s3_endpoint", "bkm_s3_bucket", "bkm_s3_chave", "bkm_s3_segredo")):
            raise RuntimeError("Preencha endpoint, bucket, access key e secret key do bucket em Recursos › Backup das mídias.")
        pasta = Path(os.path.dirname(os.path.abspath(cfg.db_path)) or ".") / "midias"
        feitos = {r["file_id"] for r in await db._todos("SELECT file_id FROM backup_midias WHERE erro IS NULL")}
        midias = await _todas_midias(db)
        res["total"] = len(midias)
        for fid, tipo in midias:
            if fid in feitos:
                continue
            ext, ctype = (".mp4", "video/mp4") if tipo == "video" else (".jpg", "image/jpeg")
            nome = hashlib.sha256(fid.encode()).hexdigest()[:24] + ext
            try:
                arq = await bot.get_file(fid)
                if (arq.file_size or 0) > galeria.LIMITE_BOT_API:
                    raise ValueError("grande")
                buf = io.BytesIO()
                await bot.download(fid, destination=buf)
                dados = buf.getvalue()
                if s3:
                    await _s3_put(conf, nome, dados, ctype)
                else:
                    pasta.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread((pasta / nome).write_bytes, dados)
                await db.conn.execute("INSERT OR REPLACE INTO backup_midias VALUES (?, ?, ?, NULL, ?)",
                                      (fid, nome, len(dados), _agora()))
                res["novos"] += 1
            except Exception as e:
                grande = str(e) == "grande" or "too big" in str(e).lower()
                res["grandes" if grande else "falhas"] += 1
                await db.conn.execute("INSERT OR REPLACE INTO backup_midias VALUES (?, ?, NULL, ?, ?)",
                                      (fid, nome, "grande" if grande else "falha", _agora()))
                if not grande:
                    log.warning("Backup de mídia falhou (%s): %s", fid[:20], e)
            await db.conn.commit()
            await asyncio.sleep(0.3)
        res["guardadas"] = (await db._um("SELECT COUNT(*) AS n FROM backup_midias WHERE erro IS NULL"))["n"]
        if avisar or res["novos"] or res["falhas"]:
            texto = (f"🗄️ <b>Backup das mídias</b> ({'bucket' if s3 else 'disco do servidor'})\n"
                     f"Novos: {res['novos']} · Guardados no total: {res['guardadas']} de {res['total']}"
                     + (f"\n⚠️ {res['grandes']} acima de 20 MB (o bot não consegue baixar)" if res["grandes"] else "")
                     + (f"\n⚠️ {res['falhas']} falharam (tento de novo amanhã)" if res["falhas"] else ""))
            if chat_id:
                await bot.send_message(chat_id, texto)
            else:
                await _avisar_casa(bot, db, cfg, texto)
    except Exception as e:
        log.exception("Backup das mídias falhou")
        msg = f"⚠️ Backup das mídias falhou: {html.escape(str(e))}"
        try:
            if chat_id:
                await bot.send_message(chat_id, msg)
            else:
                await _avisar_casa(bot, db, cfg, msg)
        except Exception:
            pass
        res["erro"] = str(e)
    finally:
        _backup_rodando = False
    return res


# ════════════════════════════════════════════════════════════════════════════
#  Bot: links de divulgação (/start l_X), indicação (/start r_ID) e "não avisar mais"
# ════════════════════════════════════════════════════════════════════════════
def get_router(cfg: Config) -> Router:
    router = Router(name="extras")
    router.message.filter(F.chat.type == "private")

    @router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^(l|r)_[A-Za-z0-9]{1,40}$")))
    async def start_rastreado(msg: Message, command: CommandObject, db: Database):
        tipo, valor = command.args.split("_", 1)
        user = msg.from_user
        novo = await db.get_usuario(user.id) is None
        await db.registrar_usuario(user.id, user.full_name, user.username)
        if tipo == "l":
            link = await db._um("SELECT codigo FROM links WHERE codigo = ? AND ativo = 1", (valor.upper(),))
            if link:
                await db.conn.execute("UPDATE usuarios SET origem = ?, origem_em = ? WHERE user_id = ? AND origem IS NULL",
                                      (link["codigo"], _agora(), user.id))
                await db.conn.commit()
        elif tipo == "r" and valor.isdigit() and novo and int(valor) != user.id and await R.valor(db, "indic_ativo"):
            if await db.get_usuario(int(valor)):
                await db.conn.execute("UPDATE usuarios SET indicado_por = ? WHERE user_id = ? AND indicado_por IS NULL",
                                      (int(valor), user.id))
                await db.conn.commit()
        import client_handlers
        await client_handlers.mostrar_perfil(msg, user, db, cfg)

    @router.callback_query(F.data == "silenciar")
    async def silenciar(cb: CallbackQuery, db: Database):
        await db.conn.execute("UPDATE usuarios SET silenciado = 1 WHERE user_id = ?", (cb.from_user.id,))
        await db.conn.commit()
        await cb.answer("Pronto, não aviso mais. Dá para religar em Minhas, na loja.", show_alert=True)

    return router
