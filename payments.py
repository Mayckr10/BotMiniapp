"""
Integração PIX com estrutura de "adaptador":

    PixGateway (interface)
      ├── MockPixGateway      -> para testes (gera BR Code + QR Code localmente)
      ├── PushinPayGateway    -> gateway real (Pushin Pay), com split para a plataforma
      ├── MercadoPagoGateway  -> gateway real (Mercado Pago marketplace): a criadora conecta a conta dela por
                                 OAuth e a taxa da plataforma (application_fee) cai na conta dona da aplicação
      └── GatewaySeletor      -> o que o bot usa: Pushin Pay com o token que a criadora cadastrou no
                                 painel (ou PUSHINPAY_TOKEN); sem token e com PIX_GATEWAY=mock, modo teste.

Split da plataforma (Pushin Pay): a cobrança é criada com o token da CRIADORA (o dinheiro cai na conta
dela) e `split_rules` manda PLATAFORMA_PCT% (padrão 10) para a conta PLATAFORMA_ACCOUNT_ID.
A Pushin Pay limita o split a 50% da transação e cobra a taxa dela da parte da conta principal.

Para trocar de gateway (Asaas, OpenPix, Mercado Pago...), basta criar uma nova
classe com os mesmos 3 métodos e registrá-la em `criar_gateway()`.
"""
import base64
import os
import secrets
from datetime import datetime, timedelta, timezone
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
    taxa: int = 0           # centavos que vão para a plataforma (split)


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
def taxa_plataforma(cfg: Config, valor: int, conta: str | None = None, pct: int | None = None) -> int:
    """Centavos do split da plataforma (0 se não houver conta configurada). Máximo 50% (limite da Pushin Pay)."""
    conta = cfg.plataforma_account_id if conta is None else conta
    pct = cfg.plataforma_pct if pct is None else pct
    if not conta or pct <= 0:
        return 0
    return valor * min(50, pct) // 100


class PushinPayGateway(PixGateway):
    BASE = "https://api.pushinpay.com.br/api"

    def __init__(self, cfg: Config, token: str | None = None):
        self.cfg = cfg
        self.http = httpx.AsyncClient(
            base_url=self.BASE,
            timeout=20,
            headers={"Authorization": f"Bearer {token or cfg.pushinpay_token}",
                     "Accept": "application/json", "Content-Type": "application/json"},
        )

    async def criar_cobranca(self, valor: int, descricao: str, referencia: str, validade_seg: int = 3600,
                             split_conta: str | None = None, split_pct: int | None = None) -> Cobranca:
        # A validade é controlada pelo próprio bot (vendas.expira_em). Se o seu gateway aceitar um
        # campo de expiração na criação da cobrança, envie `validade_seg` aqui também.
        corpo = {"value": valor, "webhook_url": self.cfg.webhook_url}
        conta = self.cfg.plataforma_account_id if split_conta is None else split_conta
        taxa = taxa_plataforma(self.cfg, valor, conta, split_pct)
        if taxa >= 1:
            corpo["split_rules"] = [{"value": taxa, "account_id": conta}]
        r = await self.http.post("/pix/cashIn", json=corpo)
        r.raise_for_status()
        dados = r.json()
        codigo = dados["qr_code"]
        b64 = dados.get("qr_code_base64", "")
        qr = base64.b64decode(b64.split(",", 1)[-1]) if b64 else gerar_qr_png(codigo)
        return Cobranca(pix_id=str(dados["id"]), copia_e_cola=codigo, qr_png=qr, valor=valor, taxa=taxa if taxa >= 1 else 0)

    async def token_valido(self) -> bool | None:
        """True/False se der para saber; None se a Pushin Pay não respondeu."""
        try:
            r = await self.http.get("/transactions/00000000-0000-0000-0000-000000000000")
        except httpx.HTTPError:
            return None
        return r.status_code not in (401, 403)

    async def consultar_pago(self, pix_id: str) -> bool:
        r = await self.http.get(f"/transactions/{pix_id}")
        r.raise_for_status()
        return str(r.json().get("status", "")).lower() == "paid"

    def ler_webhook(self, payload: dict) -> EventoWebhook:
        return EventoWebhook(pix_id=str(payload.get("id", "")),
                             pago=str(payload.get("status", "")).lower() == "paid", bruto=payload)


class MercadoPagoGateway(PixGateway):
    """PIX pelo Mercado Pago com o access_token da criadora (OAuth). O dinheiro cai na conta dela e a
    `application_fee` vai para a conta dona da aplicação (o dono da plataforma)."""
    BASE = os.getenv("MP_API_BASE", "https://api.mercadopago.com")      # trocado só nos testes
    PREFIXO = "mp_"           # o pix_id guardado nas vendas: mp_<id do pagamento>

    def __init__(self, cfg: Config, token: str):
        self.cfg = cfg
        self.http = httpx.AsyncClient(base_url=self.BASE, timeout=20,
                                      headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})

    async def criar_cobranca(self, valor: int, descricao: str, referencia: str, validade_seg: int = 3600,
                             taxa: int = 0) -> Cobranca:
        vence = datetime.now(timezone(timedelta(hours=-3))) + timedelta(seconds=max(validade_seg, 30 * 60))
        corpo = {"transaction_amount": round(valor / 100, 2), "description": descricao[:200] or "Pedido",
                 "payment_method_id": "pix", "external_reference": referencia,
                 "payer": {"email": f"comprador.{referencia[:24]}@pagamento.app".lower()},
                 "notification_url": self.cfg.webhook_url,
                 "date_of_expiration": vence.isoformat(timespec="milliseconds")}
        taxa = min(taxa, valor // 2)
        if taxa >= 1:
            corpo["application_fee"] = round(taxa / 100, 2)
        r = await self.http.post("/v1/payments", json=corpo,
                                 headers={"X-Idempotency-Key": f"{referencia}-{secrets.token_hex(6)}"})
        r.raise_for_status()
        dados = r.json()
        tx = (dados.get("point_of_interaction") or {}).get("transaction_data") or {}
        codigo = tx["qr_code"]
        b64 = tx.get("qr_code_base64", "")
        qr = base64.b64decode(b64) if b64 else gerar_qr_png(codigo)
        return Cobranca(pix_id=f"{self.PREFIXO}{dados['id']}", copia_e_cola=codigo, qr_png=qr, valor=valor,
                        taxa=taxa if taxa >= 1 else 0)

    async def consultar_pago(self, pix_id: str) -> bool:
        r = await self.http.get(f"/v1/payments/{pix_id.removeprefix(self.PREFIXO)}")
        r.raise_for_status()
        return r.json().get("status") == "approved"

    async def conta(self) -> dict | None:
        """Dados da conta conectada (id, apelido, e-mail) ou None se o token não vale mais."""
        try:
            r = await self.http.get("/users/me")
        except httpx.HTTPError:
            return None
        return r.json() if r.status_code == 200 else None

    def ler_webhook(self, payload: dict) -> EventoWebhook:
        return ler_webhook_mp(payload)


def ler_webhook_mp(payload: dict) -> EventoWebhook | None:
    """Aviso do Mercado Pago: {"type": "payment", "data": {"id": "123"}} (ou o formato antigo, topic/resource).
    O aviso não traz o status: `pago=True` só manda o bot consultar o pagamento na API, que é quem decide."""
    tipo = payload.get("type") or payload.get("topic") or ""
    if tipo != "payment":
        return None
    dados = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    pid = str(dados.get("id") or payload.get("data.id") or payload.get("id") or
              str(payload.get("resource", "")).rstrip("/").rsplit("/", 1)[-1])
    if not pid.isdigit():
        return EventoWebhook(pix_id="", pago=False, bruto=payload)
    return EventoWebhook(pix_id=f"{MercadoPagoGateway.PREFIXO}{pid}", pago=True, bruto=payload)


class GatewaySeletor(PixGateway):
    """
    Escolhe o gateway a cada cobrança:
      - token da Pushin Pay cadastrado pela criadora no painel (tabela ajustes) ou PUSHINPAY_TOKEN -> Pushin Pay;
      - sem token e PIX_GATEWAY=mock -> modo teste (botão "Simular pagamento");
      - sem token e PIX_GATEWAY=pushinpay -> erro ao gerar o PIX (nunca cai no modo teste em produção).
    """

    def __init__(self, cfg: Config, db=None):
        self.cfg, self.db = cfg, db
        self.mock = MockPixGateway(cfg)
        self._pushin: dict[str, PushinPayGateway] = {}
        self._mpgw: dict[str, MercadoPagoGateway] = {}

    async def token(self) -> str:
        salvo = await self.db.get_ajuste("pushinpay_token") if self.db else None
        return salvo or self.cfg.pushinpay_token

    async def token_mp(self) -> str:
        return (await self.db.get_ajuste("mp_access_token") if self.db else None) or ""

    async def ativo(self) -> str:
        """'mercadopago' | 'pushinpay' | '' (nenhuma conta conectada). O Mercado Pago vence se os dois existirem."""
        if await self.token_mp():
            return "mercadopago"
        return "pushinpay" if await self.token() else ""

    async def em_teste(self) -> bool:
        return not await self.ativo() and self.cfg.pix_gateway != "pushinpay"

    async def _mp(self) -> MercadoPagoGateway:
        tok = await self.token_mp()
        if not tok:
            raise RuntimeError("Conta Mercado Pago não conectada no painel")
        if tok not in self._mpgw:
            self._mpgw.clear()                  # token renovado: o antigo deixa de valer
            self._mpgw[tok] = MercadoPagoGateway(self.cfg, tok)
        return self._mpgw[tok]

    async def _real(self) -> PushinPayGateway:
        tok = await self.token()
        if not tok:
            raise RuntimeError("Conta Pushin Pay não cadastrada no painel")
        if tok not in self._pushin:
            self._pushin[tok] = PushinPayGateway(self.cfg, tok)
        return self._pushin[tok]

    async def split(self) -> tuple[str, int]:
        """Conta e % do split do dono da plataforma: o que ele cadastrou com /split, ou as variáveis de ambiente."""
        conta = (await self.db.get_ajuste("split_conta") if self.db else None)
        pct = (await self.db.get_ajuste("split_pct") if self.db else None)
        return (self.cfg.plataforma_account_id if conta is None else conta,
                self.cfg.plataforma_pct if pct is None else min(50, max(0, int(pct))))

    async def criar_cobranca(self, valor: int, descricao: str, referencia: str, validade_seg: int = 3600) -> Cobranca:
        if await self.em_teste():
            return await self.mock.criar_cobranca(valor, descricao, referencia, validade_seg)
        conta, pct = await self.split()
        if await self.ativo() == "mercadopago":
            # no Mercado Pago a taxa vai sozinha para a conta dona da aplicação: não precisa do ID da conta
            return await (await self._mp()).criar_cobranca(valor, descricao, referencia, validade_seg,
                                                            valor * min(50, pct) // 100)
        return await (await self._real()).criar_cobranca(valor, descricao, referencia, validade_seg, conta, pct)

    async def consultar_pago(self, pix_id: str) -> bool:
        if pix_id.startswith("mock_"):
            return await self.em_teste() and await self.mock.consultar_pago(pix_id)
        if pix_id.startswith(MercadoPagoGateway.PREFIXO):
            return await (await self._mp()).consultar_pago(pix_id)
        return await (await self._real()).consultar_pago(pix_id)

    async def marcar_pago_teste(self, pix_id: str) -> bool:
        return await self.em_teste() and self.mock.marcar_pago_teste(pix_id)

    def ler_webhook(self, payload: dict) -> EventoWebhook:
        mp = ler_webhook_mp(payload)
        if mp:
            return mp
        return EventoWebhook(pix_id=str(payload.get("id", "")),
                             pago=str(payload.get("status", "")).lower() == "paid", bruto=payload)


def criar_gateway(cfg: Config, db=None) -> GatewaySeletor:
    return GatewaySeletor(cfg, db)
