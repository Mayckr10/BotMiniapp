"""
Monitoramento para o dono da plataforma (IDs de RESET_IDS, padrão 8086722916).

  - Alerta no Telegram quando acontece um erro no servidor (com o trecho do traceback).
    Com limite: o mesmo erro no máximo 1 vez a cada 10 min e no máximo 15 alertas por hora.
  - Aviso quando o bot liga, com a versão (commit) em produção. Se a última execução não desligou
    direito (falta de memória, travamento, queda), o aviso diz isso: foi assim que o upload de 20
    arquivos derrubava o servidor sem ninguém saber.
  - Logs em JSON no Railway, com o nível certo (antes tudo aparecia como "error" em vermelho).
"""
import asyncio
import html
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot

from config import Config
from database import Database

log = logging.getLogger(__name__)
IGNORAR = ("Failed to fetch updates", "Sleep for", "TelegramNetworkError", "ServerDisconnectedError")


class _FormatoJSON(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        dados = {"level": r.levelname.lower(), "message": r.getMessage(), "logger": r.name}
        if r.exc_info:
            dados["exception"] = self.formatException(r.exc_info)
        return json.dumps(dados, ensure_ascii=False)


def configurar_logs() -> None:
    h = logging.StreamHandler(sys.stdout)
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("LOG_JSON") == "1":
        h.setFormatter(_FormatoJSON())
    else:
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[h], force=True)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)     # cada pedido da loja poluía o log


class AlertaTelegram(logging.Handler):
    def __init__(self, bot: Bot, destinos: set[int]):
        super().__init__(level=logging.ERROR)
        self.bot, self.destinos = bot, destinos
        self.ultimos: dict[str, float] = {}
        self.na_hora: list[float] = []
        self.loop = asyncio.get_running_loop()

    def emit(self, r: logging.LogRecord) -> None:
        try:
            msg = r.getMessage()
            if r.name.startswith(__name__) or any(x in msg for x in IGNORAR):
                return
            if r.exc_info and r.exc_info[0] and r.exc_info[0].__name__ in IGNORAR:
                return
            chave = f"{r.name}:{msg[:80]}"
            agora = time.monotonic()
            self.na_hora = [t for t in self.na_hora if agora - t < 3600]
            if agora - self.ultimos.get(chave, -1e9) < 600 or len(self.na_hora) >= 15:
                return
            self.ultimos[chave] = agora
            self.na_hora.append(agora)
            detalhe = ""
            if r.exc_info:
                detalhe = "".join(traceback.format_exception(*r.exc_info))[-1500:]
            texto = (f"🚨 <b>Erro no bot</b> · <code>{html.escape(r.name)}</code>\n{html.escape(msg[:500])}"
                     + (f"\n<pre>{html.escape(detalhe)}</pre>" if detalhe else ""))
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._enviar(texto)))
        except Exception:
            pass

    async def _enviar(self, texto: str) -> None:
        for uid in self.destinos:
            try:
                await self.bot.send_message(uid, texto, disable_notification=False)
            except Exception:
                pass


async def iniciar(bot: Bot, db: Database, cfg: Config) -> None:
    """Liga o alerta de erros e avisa o dono que o bot subiu (e se a última vez caiu)."""
    if not cfg.reset_ids:
        return
    logging.getLogger().addHandler(AlertaTelegram(bot, cfg.reset_ids))
    caiu = await db.get_ajuste("desligou_ok", "1") != "1"
    await db.set_ajuste("desligou_ok", "0")
    versao = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "local")[:7]
    msg_commit = (os.getenv("RAILWAY_GIT_COMMIT_MESSAGE") or "").split("\n")[0][:80]
    hora = datetime.now(ZoneInfo(cfg.fuso_horario)).strftime("%d/%m %H:%M")
    if caiu:
        texto = ("⚠️ <b>O bot reiniciou sem desligar direito</b> (falta de memória, travamento ou queda do servidor).\n"
                 f"Já está no ar de novo · {hora} · versão <code>{versao}</code>")
    else:
        texto = (f"🔄 Bot no ar · {hora} · versão <code>{versao}</code>"
                 + (f"\n{html.escape(msg_commit)}" if msg_commit else ""))
    for uid in cfg.reset_ids:
        try:
            await bot.send_message(uid, texto, disable_notification=not caiu)
        except Exception:
            log.info("Dono %s ainda não abriu conversa com o bot", uid)


async def desligando(db: Database) -> None:
    try:
        await db.set_ajuste("desligou_ok", "1")
    except Exception:
        pass
