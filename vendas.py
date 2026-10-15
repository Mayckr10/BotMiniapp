"""
Criação de pedidos (venda + cobrança PIX), compartilhada pelo bot e pelo Mini App.

criar_pedido() faz toda a regra de negócio: confere o item, quem já comprou, aplica cupom,
registra a venda PENDENTE e gera o PIX no gateway. Quem chama só decide como mostrar.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import Config
from database import Combo, Cupom, Database, Gorjeta, Pack, agora
from payments import Cobranca, PixGateway

log = logging.getLogger(__name__)


@dataclass
class Pedido:
    status: str                       # 'ok' | 'inexistente' | 'ja_comprou' | 'erro_gateway'
    item: object = None
    packs: list[Pack] = field(default_factory=list)
    preco: int = 0
    cupom: Cupom | None = None
    cupom_invalido: str | None = None   # código digitado que não valeu
    motivo_cupom: str = ""
    cobranca: Cobranca | None = None
    venda_id: int | None = None
    expira: datetime | None = None


async def criar_pedido(db: Database, gateway: PixGateway, cfg: Config, user_id: int,
                       item_id: str, codigo_cupom: str | None = None) -> Pedido:
    item = await db.get_item(item_id)
    packs = await db.packs_do_item(item_id)
    conteudo = isinstance(item, (Pack, Combo))
    if not item or (conteudo and not packs):
        return Pedido("inexistente")

    # Quem já tem todo o conteúdo não paga de novo
    if conteudo and await db.tem_todos(user_id, [p.id for p in packs]):
        return Pedido("ja_comprou", item=item, packs=packs)
    if isinstance(item, Pack) and item.tipo == "arquivado":       # apagado da vitrine: não vende mais
        return Pedido("inexistente")

    pedido = Pedido("ok", item=item, packs=packs, preco=item.preco)
    if codigo_cupom and not isinstance(item, Gorjeta):
        cupom, motivo = await db.validar_cupom(codigo_cupom, item_id, user_id)
        if cupom:
            pedido.cupom, pedido.preco = cupom, item.preco - cupom.desconto(item.preco)
        else:
            pedido.cupom_invalido, pedido.motivo_cupom = codigo_cupom.upper(), motivo

    # 1) venda PENDENTE  2) cobrança no gateway  3) liga as duas pelo pix_id
    pedido.venda_id = await db.criar_venda(user_id, item_id, pedido.preco, valor_original=item.preco,
                                           cupom=pedido.cupom.codigo if pedido.cupom else None)
    validade = timedelta(minutes=cfg.pix_validade_minutos)
    try:
        pedido.cobranca = await gateway.criar_cobranca(valor=pedido.preco, descricao=item.titulo[:60],
                                                       referencia=f"venda_{pedido.venda_id}",
                                                       validade_seg=int(validade.total_seconds()))
    except Exception:
        log.exception("Erro ao criar cobrança PIX")
        pedido.status = "erro_gateway"
        return pedido
    pedido.expira = agora() + validade
    await db.definir_pix(pedido.venda_id, pedido.cobranca.pix_id, pedido.cobranca.copia_e_cola, pedido.expira)
    return pedido
