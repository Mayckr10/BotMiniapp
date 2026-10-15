"""
Envio em massa respeitando o limite do Telegram (usado por /broadcast e pelas mensagens PPV).

- Fica abaixo de BROADCAST_POR_SEGUNDO (~30/s é o limite do Telegram).
- TelegramRetryAfter (429): espera o tempo pedido e tenta de novo (até 3x).
- TelegramForbiddenError (403): o usuário bloqueou o bot -> marcado e ignorado nos próximos envios.
"""
import asyncio
import logging
from typing import Awaitable, Callable

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from config import Config
from database import Database

log = logging.getLogger(__name__)


async def enviar_em_massa(db: Database, cfg: Config, destinos: list[int],
                          enviar: Callable[[int], Awaitable]) -> dict:
    res = {"enviados": 0, "bloquearam": 0, "falhas": 0}
    intervalo = 1 / max(cfg.broadcast_por_segundo, 1)
    for user_id in destinos:
        for _ in range(4):
            try:
                await enviar(user_id)
                res["enviados"] += 1
                break
            except TelegramRetryAfter as e:
                log.warning("Flood control: aguardando %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TelegramForbiddenError:
                await db.marcar_bloqueado(user_id)
                res["bloquearam"] += 1
                break
            except TelegramBadRequest as e:          # chat inexistente, conta apagada...
                log.info("Envio falhou para %s: %s", user_id, e)
                if "chat not found" in str(e).lower() or "deactivated" in str(e).lower():
                    await db.marcar_bloqueado(user_id)
                res["falhas"] += 1
                break
        else:
            res["falhas"] += 1
        await asyncio.sleep(intervalo)
    return res


def resumo_envio(res: dict) -> str:
    return (f"📤 Enviados: {res['enviados']}\n🚫 Bloquearam o bot: {res['bloquearam']}\n"
            f"⚠️ Falhas: {res['falhas']}")
