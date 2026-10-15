"""
Mensagens do bot que a criadora edita pelo painel do Mini App (guardadas na tabela `ajustes`).

  boas_vindas   o /start (texto + foto opcional). Vazio = usa a bio e a foto de perfil.
  obrigado      enviada depois que o pack/combo é entregue.
  vip           boas-vindas de quem acabou de assinar (vem antes do botão do canal).

Marcadores que podem ser usados no texto: {nome} (primeiro nome do fã), {criadora} e {item}
(nome do pack/plano, só nas mensagens de compra). O texto é enviado como texto puro.
"""
import html

from database import Database

PADRAO = {
    "boas_vindas": "",
    "obrigado": "💖 Aproveite {item}! Obrigada pela compra.",
    "vip": "⭐ Bem-vindo(a) ao VIP, {nome}!",
}
ROTULOS = {
    "boas_vindas": "Boas-vindas (/start)",
    "obrigado": "Depois da compra",
    "vip": "Boas-vindas do VIP",
}


async def texto(db: Database, chave: str) -> str:
    return await db.get_ajuste(f"msg_{chave}", PADRAO[chave]) or PADRAO[chave]


async def foto(db: Database, chave: str) -> str | None:
    return await db.get_ajuste(f"msg_{chave}_foto")


def montar(modelo: str, **valores) -> str:
    """Troca os marcadores e escapa o HTML (o bot usa parse_mode HTML)."""
    saida = html.escape(modelo)
    for k, v in valores.items():
        saida = saida.replace("{" + k + "}", f"<b>{html.escape(str(v))}</b>" if k == "item" else html.escape(str(v)))
    return saida


async def renderizar(db: Database, chave: str, **valores) -> str:
    return montar(await texto(db, chave), **valores)


# Referências preenchidas no main.py, para as funções de entrega (que não recebem o banco)
_db: Database | None = None
_cfg = None


def configurar(db: Database, cfg) -> None:
    global _db, _cfg
    _db, _cfg = db, cfg


async def _nome_do_fa(user_id: int) -> str:
    row = await _db._um("SELECT nome FROM usuarios WHERE user_id = ?", (user_id,)) if _db else None
    return ((row["nome"] if row else "") or "").split(" ")[0] or "amor"


async def para_fa(chave: str, user_id: int, item: str = "") -> str:
    if _db is None:
        return montar(PADRAO[chave], nome="", criadora="", item=item)
    return await renderizar(_db, chave, nome=await _nome_do_fa(user_id),
                            criadora=_cfg.criadora_nome if _cfg else "", item=item)
