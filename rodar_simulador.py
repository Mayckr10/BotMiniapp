"""
Roda TUDO localmente para testes, com um único comando:

    python rodar_simulador.py

- Simulador do Telegram (falsa Bot API + interface web) em http://localhost:8081
- O bot de verdade (main.py), apontando para o simulador
- Servidor de webhook PIX (gateway mock) em http://localhost:8080

Abra http://localhost:8081 no navegador.
"""
import asyncio
import logging
import os

SIM_PORT = int(os.getenv("SIM_PORT", "8081"))

# Configuração forçada para o modo simulador (não usa o seu token real)
os.environ.update({
    "BOT_TOKEN": "123456:SIMULADOR",
    "ADMIN_IDS": "111",
    "DONO_IDS": os.getenv("DONO_IDS", "111"),
    "CHANNEL_ID": "-1001234567890",
    "VIP_CHANNEL_ID": "-1009876543210",
    "TELEGRAM_API_BASE": f"http://127.0.0.1:{SIM_PORT}",
    "PIX_GATEWAY": "mock",
    "DB_PATH": os.getenv("SIM_DB_PATH", "simulador.db"),
    "WEBHOOK_PORT": os.getenv("WEBHOOK_PORT", "8080"),
    "WEBHOOK_HOST": "127.0.0.1",
    "PUBLIC_BASE_URL": f"http://127.0.0.1:{os.getenv('WEBHOOK_PORT', '8080')}",
})
# Tempos acelerados para dar para ver no simulador:
# lembrete em 1 min, PIX expira em 2 min, cupom pessoal em 3 min
for chave, valor in {"LEMBRETE_MINUTOS": "1", "PIX_VALIDADE_MINUTOS": "2", "CUPOM_REMARKETING_MINUTOS": "3",
                     "CUPOM_REMARKETING_VALIDADE_HORAS": "1", "REMARKETING_INTERVALO_SEG": "10",
                     "BACKUP_ATIVO": "0"}.items():
    os.environ.setdefault(chave, valor)
os.environ.setdefault("BOT_WEBHOOK_URL", f"http://127.0.0.1:{os.environ['WEBHOOK_PORT']}")

from aiohttp import web  # noqa: E402

import main  # noqa: E402
from config import load_config  # noqa: E402
from simulador.server import criar_app  # noqa: E402


async def rodar():
    runner = web.AppRunner(criar_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", SIM_PORT).start()
    print(f"\n  ✅ Simulador aberto em  http://localhost:{SIM_PORT}\n")
    try:
        await main.run(load_config())
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(rodar())
