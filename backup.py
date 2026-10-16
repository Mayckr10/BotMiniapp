"""
Backup automático do SQLite.

Todo dia na BACKUP_HORA (fuso FUSO_HORARIO) o bot faz uma cópia consistente do banco
(API de backup do SQLite, segura com o bot rodando), compacta com gzip e envia como
arquivo no privado de cada admin. A criadora também pode pedir na hora com /backup.

Para restaurar: pare o bot, descompacte (gunzip backup-....db.gz) e coloque o arquivo
no lugar do DB_PATH (ex: bot.db). Depois é só iniciar o bot de novo.

Limite: bots enviam arquivos de até 50 MB. Um banco desse tamanho já tem centenas de
milhares de vendas; antes disso, mova o backup para um storage (S3, Backblaze...).
"""
import asyncio
import gzip
import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BufferedInputFile

from config import Config
from database import Database

log = logging.getLogger(__name__)


async def gerar_backup(db: Database, fuso: str = "America/Sao_Paulo") -> tuple[bytes, str]:
    """Retorna (conteúdo .db.gz, nome do arquivo)."""
    with tempfile.TemporaryDirectory() as tmp:
        destino = Path(tmp, "copia.db")
        await db.backup(str(destino))
        dados = await asyncio.to_thread(lambda: gzip.compress(destino.read_bytes(), compresslevel=9))
    nome = f"backup-{datetime.now(ZoneInfo(fuso)):%Y-%m-%d_%H%M}.db.gz"
    return dados, nome


async def enviar_backup(bot: Bot, db: Database, cfg: Config, destinos: list[int] | None = None) -> int:
    dados, nome = await gerar_backup(db, cfg.fuso_horario)
    stats = await db.stats_periodo(datetime.now(ZoneInfo(cfg.fuso_horario)) - timedelta(days=1))
    legenda = (f"🗄️ <b>Backup do banco</b> · {len(dados) / 1024:.0f} KB\n"
               f"Últimas 24h: {stats['vendas']} venda(s).\n"
               "Guarde este arquivo. Para restaurar, veja o README.")
    enviados, sem_chat = 0, []
    # o dono da plataforma sempre recebe também: é quem consegue agir se a criadora não tiver conversa com o bot
    for admin_id in destinos or sorted(cfg.admin_ids | cfg.dono_ids):
        try:
            await bot.send_document(admin_id, BufferedInputFile(dados, filename=nome), caption=legenda,
                                    disable_notification=True)
            enviados += 1
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            # "chat not found": a pessoa nunca abriu o bot (ou bloqueou). Não é erro de código, então sem traceback.
            sem_chat.append(admin_id)
            log.warning("Backup não entregue a %s: %s", admin_id, e)
        except Exception:
            log.exception("Não consegui enviar o backup para %s", admin_id)
    if sem_chat and not destinos:
        for dono in cfg.dono_ids - set(sem_chat):
            try:
                await bot.send_message(dono, "⚠️ O backup diário não chegou para "
                                       + ", ".join(f"<code>{i}</code>" for i in sem_chat) +
                                       ". Essas contas precisam abrir o bot e mandar /start.")
            except Exception:
                pass
    return enviados


def segundos_ate(hora: int, fuso: str) -> float:
    agora = datetime.now(ZoneInfo(fuso))
    alvo = agora.replace(hour=hora, minute=0, second=0, microsecond=0)
    if alvo <= agora:
        alvo += timedelta(days=1)
    return (alvo - agora).total_seconds()


async def loop_backup(bot: Bot, db: Database, cfg: Config) -> None:
    log.info("Backup diário às %02d:00 (%s)", cfg.backup_hora, cfg.fuso_horario)
    while True:
        await asyncio.sleep(segundos_ate(cfg.backup_hora, cfg.fuso_horario))
        try:
            await enviar_backup(bot, db, cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erro no backup automático")
        await asyncio.sleep(60)   # evita rodar duas vezes no mesmo minuto
