"""
Integração PIX com estrutura de "adaptador":

    PixGateway (interface)
      ├── MockPixGateway      -> para testes (gera BR Code + QR Code localmente)
      └── PushinPayGateway    -> exemplo de gateway real (Pushin Pay)

Para trocar de gateway (Asaas, OpenPix, Mercado Pago...), basta criar uma nova
classe com os mesmos 3 métodos e registrá-la em `criar_gateway()`.
"""
import base64
import secrets
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from io import BytesIO

import httpx
import qrcode

from config import Config


@dataclass
class Cobranca:
    pix_id: str             # id da cobrança no gateway (guardado em vendas.pix_id)
    copia_e_cola: str       # código "PIX Copia e Cola" (BR Code)
    qr_png: bytes           # imagem PNG do QR Code
    valor: int              # centavos


@dataclass
class EventoWebhook:
    pix_id: str
    pago: bool
    bruto: dict = field(default_factory=dict)


def gerar_qr_png(texto: str) -> bytes:
    img = qrcode.make(texto, box_size=8, border=2)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class PixGateway(ABC):
    @abstractmethod
    async def criar_cobranca(self, valor: int, descricao: str, referencia: str, validade_seg: int = 3600) -> Cobranca:
        """Cria uma cobrança PIX dinâmica. `valor` em centavos; `validade_seg` = tempo até expirar."""

    @abstractmethod
    async def consultar_pago(self, pix_id: str) -> bool:
        """Consulta no gateway se a cobrança foi paga (usado para revalidar o webhook)."""

    @abstractmethod
    def ler_webhook(self, payload: dict) -> EventoWebhook:
        """Converte o JSON/form recebido no webhook para um formato padrão."""


# ════════════════════════════════════════════════════════════════════════════
#  MOCK
# ════════════════════════════════════════════════════════════════════════════
def _emv(id_: str, valor: str) -> str:
    return f"{id_}{len(valor):02d}{valor}"


def _crc16(payload: str) -> str:
    """CRC16-CCITT (0xFFFF), exigido no campo 63 do BR Code."""
    crc = 0xFFFF
    for byte in payload.encode():
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _ascii(txt: str, limite: int) -> str:
    sem_acento = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return sem_acento.upper()[:limite]


def gerar_br_code(chave: str, valor: int, nome: str, cidade: str, txid: str) -> str:
    """Monta um BR Code (PIX Copia e Cola) válido no padrão EMV do Banco Central."""
    conta = _emv("00", "br.gov.bcb.pix") + _emv("01", chave)
    payload = (
        _emv("00", "01")
        + _emv("26", conta)
        + _emv("52", "0000")
        + _emv("53", "986")
        + _emv("54", f"{valor / 100:.2f}")
        + _emv("58", "BR")
        + _emv("59", _ascii(nome, 25))
        + _emv("60", _ascii(cidade, 15))
        + _emv("62", _emv("05", txid[:25]))
        + "6304"
    )
    return payload + _crc16(payload)


class MockPixGateway(PixGateway):
    """
    Gateway falso: gera o PIX localmente e guarda o status em memória.
    Para "pagar", chame POST /mock/pagar/{pix_id} no servidor de webhook
    (o simulador web tem um botão para isso).
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cobrancas: dict[str, dict] = {}

    async def criar_cobranca(self, valor: int, descricao: str, referencia: str, validade_seg: int = 3600) -> Cobranca:
        pix_id = f"mock_{secrets.token_hex(6)}"
        codigo = gerar_br_code(self.cfg.pix_chave_mock, valor, self.cfg.pix_nome, self.cfg.pix_cidade, pix_id.replace("_", ""))
        self.cobrancas[pix_id] = {"pix_id": pix_id, "valor": valor, "descricao": descricao,
                                  "referencia": referencia, "status": "pending", "validade_seg": validade_seg}
        return Cobranca(pix_id=pix_id, copia_e_cola=codigo, qr_png=gerar_qr_png(codigo), valor=valor)

    async def consultar_pago(self, pix_id: str) -> bool:
        return self.cobrancas.get(pix_id, {}).get("status") == "paid"

    def marcar_pago_teste(self, pix_id: str) -> bool:
        """Simula o PIX caindo (botão de teste no bot e na loja). False se a cobrança não existe."""
        if pix_id not in self.cobrancas:
            return False
        self.cobrancas[pix_id]["status"] = "paid"
        return True

    def ler_webhook(self, payload: dict) -> EventoWebhook:
        # Formato do mock, igual ao enviado por /mock/pagar: {"id": "...", "status": "paid"}
        return EventoWebhook(pix_id=str(payload.get("id", "")),
                             pago=str(payload.get("status", "")).lower() == "paid", bruto=payload)


# ════════════════════════════════════════════════════════════════════════════
#  PUSHIN PAY (exemplo de gateway real)
#  Endpoints/campos baseados na documentação pública da Pushin Pay.
#  CONFIRA na documentação oficial antes de ir para produção (podem mudar).
# ════════════════════════════════════════════════════════════════════════════
class PushinPayGateway(PixGateway):
    BASE = "https://api.pushinpay.com.br/api"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = httpx.AsyncClient(
            base_url=self.BASE,
            timeout=20,
            headers={"Authorization": f"Bearer {cfg.pushinpay_token}",
                     "Accept": "application/json", "Content-Type": "application/json"},
        )

    async def criar_cobranca(self, valor: int, descricao: str, referencia: str, validade_seg: int = 3600) -> Cobranca:
        # A validade é controlada pelo próprio bot (vendas.expira_em). Se o seu gateway aceitar um
        # campo de expiração na criação da cobrança, envie `validade_seg` aqui também.
        r = await self.http.post("/pix/cashIn", json={"value": valor, "webhook_url": self.cfg.webhook_url})
        r.raise_for_status()
        dados = r.json()
        codigo = dados["qr_code"]
        b64 = dados.get("qr_code_base64", "")
        qr = base64.b64decode(b64.split(",", 1)[-1]) if b64 else gerar_qr_png(codigo)
        return Cobranca(pix_id=str(dados["id"]), copia_e_cola=codigo, qr_png=qr, valor=valor)

    async def consultar_pago(self, pix_id: str) -> bool:
        r = await self.http.get(f"/transactions/{pix_id}")
        r.raise_for_status()
        return str(r.json().get("status", "")).lower() == "paid"

    def ler_webhook(self, payload: dict) -> EventoWebhook:
        return EventoWebhook(pix_id=str(payload.get("id", "")),
                             pago=str(payload.get("status", "")).lower() == "paid", bruto=payload)


def criar_gateway(cfg: Config) -> PixGateway:
    if cfg.pix_gateway == "pushinpay":
        return PushinPayGateway(cfg)
    return MockPixGateway(cfg)
