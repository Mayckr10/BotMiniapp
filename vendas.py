"""
Criação de pedidos (venda + cobrança PIX), compartilhada pelo bot e pelo Mini App.

criar_pedido() faz toda a regra de negócio: confere o item, quem já comprou, aplica cupom,
registra a venda PENDENTE e gera o PIX no gateway. Quem chama só decide como mostrar.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import Config
from database import Carrinho, Combo, Cupom, Database, Gorjeta, Pack, PedidoPersonalizado, Plano, agora
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
    promo: str | None = None            # desconto automático aplicado (ex: 1º mês do VIP)


async def desconto_primeiro_mes(db: Database, user_id: int, plano: Plano) -> int:
    """% de desconto do 1º mês do VIP para quem nunca assinou (0 = não vale). Só planos de até 31 dias."""
    pct = int(await db.get_ajuste("vip_1mes_pct", "0") or 0)
    if pct <= 0 or plano.dias > 31 or await db.ja_assinou(user_id):
        return 0
    return min(pct, 90)


def aplicar_pct(preco: int, pct: int) -> int:
    return max(100, preco - preco * pct // 100)


async def criar_pedido(db: Database, gateway: PixGateway, cfg: Config, user_id: int,
                       item_id: str, codigo_cupom: str | None = None) -> Pedido:
    if await db.esta_banido(user_id):
        return Pedido("inexistente")
    item = await db.get_item(item_id)
    packs = await db.packs_do_item(item_id)
    conteudo = isinstance(item, (Pack, Combo, Carrinho))
    if not item or (conteudo and not packs):
        return Pedido("inexistente")
    # carrinho e pedido personalizado são de um fã só; o carrinho não vale se algum pack saiu da vitrine
    if isinstance(item, (Carrinho, PedidoPersonalizado)) and item.user_id != user_id:
        return Pedido("inexistente")
    if isinstance(item, Carrinho) and (len(packs) != len(item.pack_ids) or any(p.tipo != "pack" for p in packs)):
        return Pedido("inexistente")
    if isinstance(item, PedidoPersonalizado) and item.status != "AGUARDANDO":
        return Pedido("inexistente")              # já pago, entregue ou recusado: não gera outro PIX

    # Quem já tem todo o conteúdo não paga de novo
    if conteudo and await db.tem_todos(user_id, [p.id for p in packs]):
        return Pedido("ja_comprou", item=item, packs=packs)
    if isinstance(item, Pack) and item.tipo in ("arquivado", "fila", "agendado", "pedido", "preparando"):       # apagado da vitrine: não vende mais
        return Pedido("inexistente")

    pedido = Pedido("ok", item=item, packs=packs, preco=item.preco)
    if codigo_cupom and not isinstance(item, (Gorjeta, PedidoPersonalizado)):
        cupom, motivo = await db.validar_cupom(codigo_cupom, item_id, user_id)
        if cupom:
            pedido.cupom, pedido.preco = cupom, item.preco - cupom.desconto(item.preco)
        else:
            pedido.cupom_invalido, pedido.motivo_cupom = codigo_cupom.upper(), motivo
    if isinstance(item, Plano) and not pedido.cupom:
        pct = await desconto_primeiro_mes(db, user_id, item)
        if pct:
            pedido.preco, pedido.promo = aplicar_pct(item.preco, pct), f"1º mês com {pct}% OFF"

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
