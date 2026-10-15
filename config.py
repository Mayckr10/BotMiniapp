"""
Configuração central do bot, lida de variáveis de ambiente (ou arquivo .env).
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _ids(valor: str) -> set[int]:
    return {int(x) for x in valor.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    # Telegram
    bot_token: str = os.getenv("BOT_TOKEN", "")
    # ADMIN_IDS_EXTRA soma admins sem precisar reescrever ADMIN_IDS
    admin_ids: set[int] = field(default_factory=lambda: _ids(os.getenv("ADMIN_IDS", "")) |
                                                        _ids(os.getenv("ADMIN_IDS_EXTRA", "")))
    # Quem pode usar /resetar_banco (apaga tudo). Só esses IDs veem o comando.
    reset_ids: set[int] = field(default_factory=lambda: _ids(os.getenv("RESET_IDS", "8086722916")))
    channel_id: int = int(os.getenv("CHANNEL_ID", "0"))
    # Base alternativa da Bot API (usada pelo simulador local). Vazio = api.telegram.org
    telegram_api_base: str = os.getenv("TELEGRAM_API_BASE", "")

    # Servidor HTTP que recebe o webhook do gateway PIX
    webhook_host: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
    webhook_port: int = int(os.getenv("WEBHOOK_PORT", "8080"))
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "troque-este-segredo")

    # PIX
    pix_gateway: str = os.getenv("PIX_GATEWAY", "mock").lower()
    pushinpay_token: str = os.getenv("PUSHINPAY_TOKEN", "")      # opcional: a criadora cadastra o dela no painel
    # Split da plataforma: % de cada venda que vai para a conta Pushin Pay da plataforma (máx. 50%, regra da Pushin Pay)
    plataforma_account_id: str = os.getenv("PLATAFORMA_ACCOUNT_ID", "")
    plataforma_pct: int = min(50, max(0, int(os.getenv("PLATAFORMA_PCT", "10"))))
    pix_chave_mock: str = os.getenv("PIX_CHAVE_MOCK", "contato@exemplo.com")
    pix_nome: str = os.getenv("PIX_NOME_RECEBEDOR", "CRIADORA DE CONTEUDO")
    pix_cidade: str = os.getenv("PIX_CIDADE_RECEBEDOR", "SAO PAULO")

    # Criadora (tudo que muda de uma criadora para outra fica aqui ou nos comandos /perfil e /plano)
    criadora_nome: str = os.getenv("CRIADORA_NOME", "Criadora")
    vip_channel_id: int = int(os.getenv("VIP_CHANNEL_ID", "0"))         # canal privado dos assinantes
    planos_padrao: str = os.getenv("PLANOS_PADRAO", "Mensal:30:29,90;Trimestral:90:79,90;Semestral:180:149,90")
    assinatura_aviso_dias: float = float(os.getenv("ASSINATURA_AVISO_DIAS", "3"))
    gorjeta_valores: str = os.getenv("GORJETA_VALORES", "10,25,50,100")

    # Mini App da loja (abre em PUBLIC_BASE_URL/loja; o Telegram exige HTTPS)
    loja_ativa: bool = os.getenv("LOJA_ATIVA", "1") != "0"

    # PIX e maioridade
    pix_validade_minutos: float = float(os.getenv("PIX_VALIDADE_MINUTOS", "60"))
    exigir_maioridade: bool = os.getenv("EXIGIR_MAIORIDADE", "1") != "0"
    termos_url: str = os.getenv("TERMOS_URL", "")

    # Backup automático do SQLite (enviado no privado dos admins)
    backup_ativo: bool = os.getenv("BACKUP_ATIVO", "1") != "0"
    backup_hora: int = int(os.getenv("BACKUP_HORA", "4"))          # todo dia, hora local (FUSO_HORARIO)
    resumo_hora: int = int(os.getenv("RESUMO_HORA", "22"))         # resumo do dia no chat da criadora

    # Remarketing de PIX pendente
    lembrete_minutos: float = float(os.getenv("LEMBRETE_MINUTOS", "30"))
    cupom_remarketing_minutos: float = float(os.getenv("CUPOM_REMARKETING_MINUTOS", "1440"))   # 24h
    cupom_remarketing_pct: int = int(os.getenv("CUPOM_REMARKETING_PCT", "15"))
    cupom_remarketing_validade_horas: float = float(os.getenv("CUPOM_REMARKETING_VALIDADE_HORAS", "48"))
    remarketing_intervalo_seg: float = float(os.getenv("REMARKETING_INTERVALO_SEG", "60"))

    # Broadcast: o Telegram aceita ~30 msg/s no total; ficamos abaixo disso
    broadcast_por_segundo: float = float(os.getenv("BROADCAST_POR_SEGUNDO", "25"))
    fuso_horario: str = os.getenv("FUSO_HORARIO", "America/Sao_Paulo")

    # Outros
    db_path: str = os.getenv("DB_PATH", "bot.db")
    blur_radius: int = int(os.getenv("BLUR_RADIUS", "20"))

    @property
    def webhook_url(self) -> str:
        """URL completa enviada ao gateway (o ?token= protege a rota contra chamadas falsas)."""
        return f"{self.public_base_url.rstrip('/')}/webhook/pix?token={self.webhook_secret}"


def parse_planos(texto: str) -> list[tuple[str, int, int]]:
    """'Mensal:30:29,90;Trimestral:90:79,90' -> [('Mensal', 30, 2990), ('Trimestral', 90, 7990)]"""
    planos = []
    for parte in filter(None, (p.strip() for p in texto.split(";"))):
        nome, dias, preco = parte.split(":")
        planos.append((nome.strip(), int(dias), round(float(preco.replace(".", "").replace(",", ".")) * 100)))
    return planos


def load_config() -> Config:
    cfg = Config()
    if not cfg.bot_token:
        raise RuntimeError("Defina BOT_TOKEN no .env")
    if not cfg.admin_ids:
        raise RuntimeError("Defina ADMIN_IDS no .env")
    if not cfg.channel_id:
        raise RuntimeError("Defina CHANNEL_ID no .env")
    return cfg
