"""
Camada de banco de dados (SQLite assíncrono via aiosqlite).

Tabelas:
  packs    -> produtos à venda (mídias originais + mídias embaçadas)
  combos   -> vários packs vendidos juntos por um preço especial
  vendas   -> cada tentativa de compra (PENDENTE -> PAGO)
  acessos  -> quais packs cada cliente já liberou (pack avulso ou via combo)
  cupons   -> descontos aplicados via deep link (/start pack_8f3a_CUPOM10)
  usuarios -> todo mundo que já deu /start (base para broadcast e métricas)
  cliques  -> cada abertura de link de venda (para a taxa de conversão)

As listas de mídias são guardadas como JSON:
  [{"tipo": "photo", "file_id": "AgAC..."}, {"tipo": "video", "file_id": "BAAC..."}]
Valores em dinheiro são guardados em CENTAVOS (inteiro).
Datas são ISO-8601 em UTC ("2026-10-15T07:17:00+00:00"), então dá para comparar como texto.

"Item" = algo que se compra: um pack (pack_xxxx) ou um combo (combo_xxxx).
"""
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS packs (
    id                  TEXT PRIMARY KEY,          -- ex: pack_8f3a
    titulo              TEXT    NOT NULL,
    preco               INTEGER NOT NULL,          -- em centavos (2990 = R$ 29,90)
    file_ids_originais  TEXT    NOT NULL,          -- JSON com as mídias originais
    file_ids_blur       TEXT    NOT NULL,          -- JSON com as mídias embaçadas (teaser)
    ativo               INTEGER NOT NULL DEFAULT 1,
    criado_em           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS combos (
    id          TEXT PRIMARY KEY,                  -- ex: combo_1a2b
    titulo      TEXT    NOT NULL,
    preco       INTEGER NOT NULL,
    pack_ids    TEXT    NOT NULL,                  -- JSON: ["pack_8f3a", "pack_77c1"]
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS vendas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    pack_id     TEXT    NOT NULL,                      -- id do item: pack_xxxx ou combo_xxxx
    status      TEXT    NOT NULL DEFAULT 'PENDENTE',   -- PENDENTE | PAGO
    pix_id      TEXT    UNIQUE,                        -- id da cobrança no gateway
    valor       INTEGER NOT NULL,                      -- centavos cobrados (já com desconto)
    criado_em   TEXT    NOT NULL,
    pago_em     TEXT
);

CREATE TABLE IF NOT EXISTS acessos (
    user_id     INTEGER NOT NULL,
    pack_id     TEXT    NOT NULL,
    venda_id    INTEGER,
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (user_id, pack_id)
);

CREATE TABLE IF NOT EXISTS cupons (
    codigo      TEXT PRIMARY KEY,                  -- sempre MAIÚSCULO
    tipo        TEXT    NOT NULL,                  -- 'pct' (percentual) | 'fixo' (centavos)
    valor       INTEGER NOT NULL,                  -- 10 = 10%  |  500 = R$ 5,00
    item_id     TEXT,                              -- NULL = vale para qualquer pack/combo
    user_id     INTEGER,                           -- NULL = qualquer cliente (remarketing usa cupom pessoal)
    usos_max    INTEGER,                           -- NULL = ilimitado
    usos        INTEGER NOT NULL DEFAULT 0,
    expira_em   TEXT,                              -- NULL = não expira
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS usuarios (
    user_id     INTEGER PRIMARY KEY,
    nome        TEXT,
    username    TEXT,
    bloqueado   INTEGER NOT NULL DEFAULT 0,        -- 1 = bloqueou o bot (detectado no broadcast)
    criado_em   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cliques (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    item_id     TEXT    NOT NULL,
    criado_em   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS planos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nome        TEXT    NOT NULL,                  -- ex: Mensal
    dias        INTEGER NOT NULL,                  -- 30, 90, 180...
    preco       INTEGER NOT NULL,                  -- centavos
    ativo       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS assinaturas (
    user_id     INTEGER PRIMARY KEY,
    plano_id    INTEGER,
    inicio_em   TEXT    NOT NULL,
    expira_em   TEXT    NOT NULL,
    aviso_etapa INTEGER NOT NULL DEFAULT 0,        -- 0 nada | 1 aviso de vencimento enviado
    status      TEXT    NOT NULL DEFAULT 'ATIVA'   -- ATIVA | VENCIDA
);

CREATE TABLE IF NOT EXISTS chat_relay (
    admin_chat_id INTEGER NOT NULL,
    admin_msg_id  INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    PRIMARY KEY (admin_chat_id, admin_msg_id)
);

CREATE TABLE IF NOT EXISTS ajustes (
    chave       TEXT PRIMARY KEY,                  -- bio, foto_perfil, link_vip...
    valor       TEXT
);

-- Publicações de texto do feed (com foto opcional, sempre visíveis para todos)
CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    texto       TEXT    NOT NULL,
    foto        TEXT,                              -- file_id da foto (opcional)
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS curtidas (
    user_id     INTEGER NOT NULL,
    alvo        TEXT    NOT NULL,                  -- pack_xxxx, combo_xxxx ou post_N
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (user_id, alvo)
);

-- Quantas vezes o fã pediu para receber de novo no chat um pack já comprado (limite anti-repasse)
CREATE TABLE IF NOT EXISTS reenvios (
    user_id     INTEGER NOT NULL,
    pack_id     TEXT    NOT NULL,
    n           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, pack_id)
);

-- Enquetes: um voto por fã por post (o post guarda as opções em posts.enquete)
CREATE TABLE IF NOT EXISTS votos (
    post_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    opcao       INTEGER NOT NULL,
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (post_id, user_id)
);

-- "Me avise" de um lançamento agendado: o bot manda mensagem quando o pack sai
CREATE TABLE IF NOT EXISTS avisos_lancamento (
    pack_id     TEXT    NOT NULL,
    user_id     INTEGER NOT NULL,
    criado_em   TEXT    NOT NULL,
    PRIMARY KEY (pack_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_vendas_user_pack ON vendas(user_id, pack_id, status);
CREATE INDEX IF NOT EXISTS idx_vendas_status ON vendas(status, criado_em);
CREATE INDEX IF NOT EXISTS idx_cliques_data ON cliques(criado_em);
"""

# Colunas adicionadas depois do MVP: aplicadas com ALTER TABLE em bancos antigos
MIGRACOES = {
    "vendas": {
        "valor_original": "INTEGER",                    # preço antes do cupom
        "cupom": "TEXT",                                # código usado
        "pix_copia_cola": "TEXT",                       # para reenviar no lembrete
        "remarketing_etapa": "INTEGER NOT NULL DEFAULT 0",  # 0 nada | 1 lembrete | 2 cupom enviado
        "expira_em": "TEXT",                            # validade do PIX
    },
    "posts": {
        "status": "TEXT NOT NULL DEFAULT 'publicado'",  # publicado | fila (aguardando revisão) | agendado
        "enquete": "TEXT",                              # JSON com as opções (NULL = sem enquete)
        "agendado_para": "TEXT",                        # quando publicar sozinho (status agendado)
        "video": "TEXT",                                # file_id do vídeo (a coluna foto guarda a capa)
    },
    "packs": {
        "postar_canal": "INTEGER NOT NULL DEFAULT 0",   # na fila: posta no canal quando for aprovado
        "tipo": "TEXT NOT NULL DEFAULT 'pack'",        # pack (canal/catálogo) | ppv (mensagem paga no privado)
        "legenda": "TEXT",                              # texto da postagem no feed da loja
        "agendado_para": "TEXT",                        # tipo agendado: quando o pack vai ao ar
        "contagem": "INTEGER NOT NULL DEFAULT 0",       # 1 = fãs veem a contagem regressiva antes do lançamento
    },
    "usuarios": {
        "maior_idade_em": "TEXT",                       # quando confirmou ter 18+ (NULL = não confirmou)
        "menor": "INTEGER NOT NULL DEFAULT 0",          # 1 = declarou ser menor: compra bloqueada
        "banido": "INTEGER NOT NULL DEFAULT 0",         # 1 = a criadora bloqueou este fã (sem loja, sem bot, sem VIP)
        "banido_em": "TEXT",
    },
}

STATUS_PENDENTE = "PENDENTE"
STATUS_PAGO = "PAGO"
STATUS_EXPIRADO = "EXPIRADO"     # PIX venceu sem pagamento (se o gateway confirmar depois, ainda entrega)


def agora() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _agora() -> str:
    return iso(agora())


def fmt_reais(centavos: int) -> str:
    """2990 -> 'R$ 29,90'"""
    reais = f"{centavos / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {reais}"


@dataclass
class Pack:
    id: str
    titulo: str
    preco: int
    originais: list[dict]
    blur: list[dict]
    criado_em: str = ""
    legenda: str = ""
    tipo: str = "pack"

    @property
    def preco_fmt(self) -> str:
        return fmt_reais(self.preco)


@dataclass
class Combo:
    id: str
    titulo: str
    preco: int
    pack_ids: list[str]

    @property
    def preco_fmt(self) -> str:
        return fmt_reais(self.preco)


@dataclass
class Plano:
    id: int
    nome: str
    dias: int
    preco: int

    @property
    def item_id(self) -> str:
        return f"plano_{self.id}"

    @property
    def titulo(self) -> str:
        return f"Assinatura VIP · {self.nome} ({self.dias} dias)"

    @property
    def preco_fmt(self) -> str:
        return fmt_reais(self.preco)


@dataclass
class Gorjeta:
    preco: int
    titulo: str = "Mimo 💝"
    alvo: str = ""          # pack/combo/post que recebeu o mimo (id gorjeta_<valor>_<alvo>)

    @property
    def preco_fmt(self) -> str:
        return fmt_reais(self.preco)


@dataclass
class Assinatura:
    user_id: int
    plano_id: int | None
    expira_em: str
    status: str
    aviso_etapa: int

    @property
    def ativa(self) -> bool:
        return self.status == "ATIVA" and self.expira_em > _agora()


@dataclass
class Cupom:
    codigo: str
    tipo: str
    valor: int
    item_id: str | None
    user_id: int | None
    usos_max: int | None
    usos: int
    expira_em: str | None
    ativo: bool

    def desconto(self, preco: int) -> int:
        """Quanto descontar (centavos). Nunca deixa o preço final abaixo de R$ 1,00."""
        d = preco * self.valor // 100 if self.tipo == "pct" else self.valor
        return max(0, min(d, preco - 100))

    @property
    def descricao(self) -> str:
        return f"{self.valor}% OFF" if self.tipo == "pct" else f"{fmt_reais(self.valor)} OFF"


@dataclass
class Venda:
    id: int
    user_id: int
    pack_id: str            # id do item (pack ou combo)
    status: str
    pix_id: str | None
    valor: int
    valor_original: int | None = None
    cupom: str | None = None
    pix_copia_cola: str | None = None
    remarketing_etapa: int = 0
    criado_em: str = ""
    expira_em: str | None = None


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        await self._migrar()
        await self.conn.commit()

    async def _migrar(self) -> None:
        for tabela, colunas in MIGRACOES.items():
            async with self.conn.execute(f"PRAGMA table_info({tabela})") as cur:
                existentes = {r["name"] for r in await cur.fetchall()}
            for nome, tipo in colunas.items():
                if nome not in existentes:
                    await self.conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
        # Bancos do MVP: vendas pagas viram acessos (idempotente)
        await self.conn.execute(
            "INSERT OR IGNORE INTO acessos (user_id, pack_id, venda_id, criado_em) "
            "SELECT user_id, pack_id, id, COALESCE(pago_em, criado_em) FROM vendas "
            "WHERE status = 'PAGO' AND pack_id LIKE 'pack_%'"
        )

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _um(self, sql: str, params=()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _todos(self, sql: str, params=()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ── Packs ──────────────────────────────────────────────────────────────
    async def _gerar_id(self, prefixo: str) -> str:
        """Gera um id curto e único no formato pack_xxxx / combo_xxxx."""
        while True:
            novo = f"{prefixo}_{secrets.token_hex(2)}"
            if not await self._um("SELECT 1 FROM packs WHERE id = ? UNION SELECT 1 FROM combos WHERE id = ?",
                                  (novo, novo)):
                return novo

    async def gerar_pack_id(self) -> str:
        return await self._gerar_id("pack")

    async def criar_pack(self, titulo: str, preco: int, originais: list[dict], blur: list[dict]) -> str:
        pack_id = await self.gerar_pack_id()
        await self.conn.execute(
            "INSERT INTO packs (id, titulo, preco, file_ids_originais, file_ids_blur, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pack_id, titulo, preco, json.dumps(originais), json.dumps(blur), _agora()),
        )
        await self.conn.commit()
        return pack_id

    async def get_pack(self, pack_id: str) -> Pack | None:
        row = await self._um("SELECT * FROM packs WHERE id = ? AND ativo = 1", (pack_id,))
        if not row:
            return None
        return Pack(
            id=row["id"],
            titulo=row["titulo"],
            preco=row["preco"],
            originais=json.loads(row["file_ids_originais"]),
            blur=json.loads(row["file_ids_blur"]),
            criado_em=row["criado_em"],
            legenda=row["legenda"] or "",
            tipo=row["tipo"],
        )

    async def listar_packs(self, tipo: str | None = "pack") -> list[Pack]:
        """tipo='pack' (padrão: catálogo/canal), 'ppv' (mensagens pagas) ou None (todos)."""
        sql = "SELECT id FROM packs WHERE ativo = 1" + (" AND tipo = ?" if tipo else "")
        rows = await self._todos(sql + " ORDER BY criado_em DESC, rowid DESC", (tipo,) if tipo else ())
        return [p for p in [await self.get_pack(r["id"]) for r in rows] if p]

    async def definir_tipo_pack(self, pack_id: str, tipo: str) -> None:
        await self.conn.execute("UPDATE packs SET tipo = ? WHERE id = ?", (tipo, pack_id))
        await self.conn.commit()

    # ── Combos ─────────────────────────────────────────────────────────────
    async def criar_combo(self, titulo: str, preco: int, pack_ids: list[str]) -> str:
        combo_id = await self._gerar_id("combo")
        await self.conn.execute(
            "INSERT INTO combos (id, titulo, preco, pack_ids, criado_em) VALUES (?, ?, ?, ?, ?)",
            (combo_id, titulo, preco, json.dumps(pack_ids), _agora()),
        )
        await self.conn.commit()
        return combo_id

    async def get_combo(self, combo_id: str) -> Combo | None:
        row = await self._um("SELECT * FROM combos WHERE id = ? AND ativo = 1", (combo_id,))
        if not row:
            return None
        return Combo(id=row["id"], titulo=row["titulo"], preco=row["preco"], pack_ids=json.loads(row["pack_ids"]))

    async def listar_combos(self) -> list[Combo]:
        rows = await self._todos("SELECT id FROM combos WHERE ativo = 1 ORDER BY criado_em DESC, rowid DESC")
        return [c for c in [await self.get_combo(r["id"]) for r in rows] if c]

    async def get_item(self, item_id: str) -> "Pack | Combo | Plano | Gorjeta | None":
        if item_id.startswith("combo_"):
            return await self.get_combo(item_id)
        if item_id.startswith("plano_"):
            return await self.get_plano(int(item_id[6:]))
        if item_id.startswith("gorjeta_"):
            valor, _, alvo = item_id[8:].partition("_")
            if not valor.isdigit() or int(valor) < 100:
                return None
            if not alvo:
                return Gorjeta(int(valor))
            ref = await self.get_post(int(alvo[5:])) if alvo.startswith("post_") and alvo[5:].isdigit() \
                else await self.get_item(alvo) if alvo.startswith(("pack_", "combo_")) else None
            if not ref:
                return None
            nome = ref["texto"][:30] if isinstance(ref, dict) else ref.titulo
            return Gorjeta(int(valor), f"Mimo 💝 · {nome}", alvo)
        return await self.get_pack(item_id)

    async def packs_do_item(self, item_id: str) -> list[Pack]:
        """Pack avulso -> [pack]; combo -> todos os packs ativos dele."""
        item = await self.get_item(item_id)
        if isinstance(item, Pack):
            return [item]
        if isinstance(item, Combo):
            return [p for p in [await self.get_pack(i) for i in item.pack_ids] if p]
        return []

    # ── Planos e assinaturas ───────────────────────────────────────────────
    async def criar_planos_padrao(self, planos: list[tuple[str, int, int]]) -> None:
        """Só cria se ainda não existir nenhum plano."""
        if await self._um("SELECT 1 FROM planos"):
            return
        await self.conn.executemany("INSERT INTO planos (nome, dias, preco) VALUES (?, ?, ?)", planos)
        await self.conn.commit()

    async def salvar_plano(self, nome: str, dias: int, preco: int) -> int:
        """Cria o plano ou atualiza o de mesmo nome."""
        row = await self._um("SELECT id FROM planos WHERE lower(nome) = lower(?)", (nome,))
        if row:
            await self.conn.execute("UPDATE planos SET dias = ?, preco = ?, ativo = 1 WHERE id = ?",
                                    (dias, preco, row["id"]))
            plano_id = row["id"]
        else:
            plano_id = (await self.conn.execute("INSERT INTO planos (nome, dias, preco) VALUES (?, ?, ?)",
                                                (nome, dias, preco))).lastrowid
        await self.conn.commit()
        return plano_id

    async def atualizar_plano(self, plano_id: int, nome: str, dias: int, preco: int) -> bool:
        cur = await self.conn.execute("UPDATE planos SET nome = ?, dias = ?, preco = ? WHERE id = ? AND ativo = 1",
                                      (nome, dias, preco, plano_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def desativar_plano(self, plano_id: int) -> bool:
        cur = await self.conn.execute("UPDATE planos SET ativo = 0 WHERE id = ?", (plano_id,))
        await self.conn.commit()
        return cur.rowcount == 1

    async def get_plano(self, plano_id: int, incluir_inativos: bool = False) -> Plano | None:
        sql = "SELECT * FROM planos WHERE id = ?" + ("" if incluir_inativos else " AND ativo = 1")
        row = await self._um(sql, (plano_id,))
        return Plano(row["id"], row["nome"], row["dias"], row["preco"]) if row else None

    async def listar_planos(self) -> list[Plano]:
        rows = await self._todos("SELECT * FROM planos WHERE ativo = 1 ORDER BY dias")
        return [Plano(r["id"], r["nome"], r["dias"], r["preco"]) for r in rows]

    async def get_assinatura(self, user_id: int) -> Assinatura | None:
        row = await self._um("SELECT * FROM assinaturas WHERE user_id = ?", (user_id,))
        if not row:
            return None
        return Assinatura(row["user_id"], row["plano_id"], row["expira_em"], row["status"], row["aviso_etapa"])

    async def assinante_ativo(self, user_id: int) -> bool:
        a = await self.get_assinatura(user_id)
        return bool(a and a.ativa)

    async def estender_assinatura(self, user_id: int, plano_id: int, dias: int) -> datetime:
        """Soma `dias` a partir do vencimento atual (se ainda ativa) ou de agora. Retorna o novo vencimento."""
        atual = await self.get_assinatura(user_id)
        base = agora()
        if atual and atual.ativa:
            base = datetime.fromisoformat(atual.expira_em)
        nova = base + timedelta(days=dias)
        await self.conn.execute(
            "INSERT INTO assinaturas (user_id, plano_id, inicio_em, expira_em, aviso_etapa, status) "
            "VALUES (?, ?, ?, ?, 0, 'ATIVA') ON CONFLICT(user_id) DO UPDATE SET plano_id = excluded.plano_id, "
            "expira_em = excluded.expira_em, aviso_etapa = 0, status = 'ATIVA'",
            (user_id, plano_id, _agora(), iso(nova)),
        )
        await self.conn.commit()
        return nova

    async def assinaturas_a_avisar(self, vence_antes_de: datetime, etapa: int = 1) -> list[Assinatura]:
        """Ativas que vencem antes de `vence_antes_de` e ainda não receberam o aviso desta etapa (1 = 3 dias, 2 = 1 dia)."""
        rows = await self._todos(
            "SELECT * FROM assinaturas WHERE status = 'ATIVA' AND aviso_etapa < ? AND expira_em <= ? AND expira_em > ?",
            (etapa, iso(vence_antes_de), _agora()))
        return [Assinatura(r["user_id"], r["plano_id"], r["expira_em"], r["status"], r["aviso_etapa"]) for r in rows]

    async def marcar_aviso_assinatura(self, user_id: int, etapa: int = 1) -> None:
        await self.conn.execute("UPDATE assinaturas SET aviso_etapa = ? WHERE user_id = ?", (etapa, user_id))
        await self.conn.commit()

    async def ja_assinou(self, user_id: int) -> bool:
        """Já teve alguma assinatura VIP (ativa ou vencida)? Usado no desconto do 1º mês."""
        return await self._um("SELECT 1 FROM assinaturas WHERE user_id = ?", (user_id,)) is not None

    async def assinaturas_vencidas(self) -> list[Assinatura]:
        rows = await self._todos("SELECT * FROM assinaturas WHERE status = 'ATIVA' AND expira_em <= ?", (_agora(),))
        return [Assinatura(r["user_id"], r["plano_id"], r["expira_em"], r["status"], r["aviso_etapa"]) for r in rows]

    async def vencer_agora(self, user_id: int) -> bool:
        """Faz a assinatura vencer agora (o job remove do canal na próxima rodada)."""
        cur = await self.conn.execute("UPDATE assinaturas SET expira_em = ? WHERE user_id = ? AND status = 'ATIVA'",
                                      (_agora(), user_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def encerrar_assinatura(self, user_id: int) -> None:
        await self.conn.execute("UPDATE assinaturas SET status = 'VENCIDA' WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def assinantes_ativos(self) -> list[int]:
        rows = await self._todos(
            "SELECT a.user_id FROM assinaturas a LEFT JOIN usuarios u ON u.user_id = a.user_id "
            "WHERE a.status = 'ATIVA' AND a.expira_em > ? AND COALESCE(u.bloqueado, 0) = 0 AND COALESCE(u.banido, 0) = 0 "
            "ORDER BY a.user_id",
            (_agora(),))
        return [r["user_id"] for r in rows]

    async def resumo_assinaturas(self) -> dict:
        ativos = await self._um("SELECT COUNT(*) AS n FROM assinaturas WHERE status = 'ATIVA' AND expira_em > ?",
                                (_agora(),))
        vencendo = await self._um(
            "SELECT COUNT(*) AS n FROM assinaturas WHERE status = 'ATIVA' AND expira_em > ? AND expira_em <= ?",
            (_agora(), iso(agora() + timedelta(days=7))))
        receita = await self._um(
            "SELECT COALESCE(SUM(valor), 0) AS total FROM vendas WHERE status = 'PAGO' AND pack_id LIKE 'plano_%' "
            "AND pago_em >= ?", (iso(agora() - timedelta(days=30)),))
        return {"ativos": ativos["n"], "vencendo_7d": vencendo["n"], "receita_30d": receita["total"]}

    # ── Chat com a criadora ────────────────────────────────────────────────
    async def salvar_relay(self, admin_chat_id: int, admin_msg_id: int, user_id: int) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO chat_relay VALUES (?, ?, ?)", (admin_chat_id, admin_msg_id, user_id))
        await self.conn.commit()

    async def user_do_relay(self, admin_chat_id: int, admin_msg_id: int) -> int | None:
        row = await self._um("SELECT user_id FROM chat_relay WHERE admin_chat_id = ? AND admin_msg_id = ?",
                             (admin_chat_id, admin_msg_id))
        return row["user_id"] if row else None

    # ── Ajustes (perfil da criadora etc.) ──────────────────────────────────
    async def get_ajuste(self, chave: str, padrao: str | None = None) -> str | None:
        row = await self._um("SELECT valor FROM ajustes WHERE chave = ?", (chave,))
        return row["valor"] if row else padrao

    async def set_ajuste(self, chave: str, valor: str | None) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO ajustes VALUES (?, ?)", (chave, valor))
        await self.conn.commit()

    # ── Publicações (feed) e edição de packs pelo painel da criadora ───────
    async def criar_post(self, texto: str, foto: str | None = None, enquete: list[str] | None = None,
                         video: str | None = None) -> int:
        cur = await self.conn.execute("INSERT INTO posts (texto, foto, enquete, criado_em, video) VALUES (?, ?, ?, ?, ?)",
                                      (texto, foto, json.dumps(enquete, ensure_ascii=False) if enquete else None, _agora(),
                                       video))
        await self.conn.commit()
        return cur.lastrowid

    async def listar_posts(self, status: str = "publicado") -> list[dict]:
        rows = await self._todos("SELECT id, texto, foto, video, criado_em, enquete, agendado_para FROM posts "
                                 "WHERE ativo = 1 AND status = ? ORDER BY id DESC", (status,))
        return [{**dict(r), "enquete": json.loads(r["enquete"]) if r["enquete"] else None} for r in rows]

    # ── Agendamento ────────────────────────────────────────────────────────
    async def agendar_post(self, post_id: int, quando: datetime) -> None:
        await self.conn.execute("UPDATE posts SET status = 'agendado', agendado_para = ? WHERE id = ?",
                                (iso(quando), post_id))
        await self.conn.commit()

    async def agendar_pack(self, pack_id: str, quando: datetime, contagem: bool, postar_canal: bool) -> None:
        await self.conn.execute("UPDATE packs SET tipo = 'agendado', agendado_para = ?, contagem = ?, postar_canal = ? "
                                "WHERE id = ?", (iso(quando), int(contagem), int(postar_canal), pack_id))
        await self.conn.commit()

    async def reagendar(self, tipo: str, item_id, quando: datetime) -> bool:
        tabela, cond = ("posts", "status = 'agendado'") if tipo == "post" else ("packs", "tipo = 'agendado'")
        cur = await self.conn.execute(f"UPDATE {tabela} SET agendado_para = ? WHERE id = ? AND {cond}",
                                      (iso(quando), item_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def agendados(self, so_vencidos: bool = False) -> dict:
        """{"posts": [...], "packs": [Pack + agendado_para/contagem]} na ordem em que vão ao ar."""
        filtro, params = (" AND agendado_para <= ?", (_agora(),)) if so_vencidos else ("", ())
        posts = await self._todos("SELECT id, texto, foto, video, enquete, agendado_para FROM posts WHERE ativo = 1 "
                                  "AND status = 'agendado'" + filtro + " ORDER BY agendado_para", params)
        packs = await self._todos("SELECT id, agendado_para, contagem, postar_canal FROM packs WHERE ativo = 1 "
                                  "AND tipo = 'agendado'" + filtro + " ORDER BY agendado_para", params)
        return {"posts": [{**dict(r), "enquete": json.loads(r["enquete"]) if r["enquete"] else None} for r in posts],
                "packs": [{"pack": await self.get_pack(r["id"]), "agendado_para": r["agendado_para"],
                           "contagem": bool(r["contagem"]), "postar_canal": bool(r["postar_canal"])} for r in packs]}

    async def publicar_post_agendado(self, post_id: int) -> bool:
        cur = await self.conn.execute("UPDATE posts SET status = 'publicado', criado_em = ? WHERE id = ? "
                                      "AND status = 'agendado'", (_agora(), post_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def publicar_pack_agendado(self, pack_id: str) -> tuple[bool, bool]:
        """Devolve (publicou?, pediu para postar no canal?)."""
        row = await self._um("SELECT postar_canal FROM packs WHERE id = ? AND tipo = 'agendado' AND ativo = 1", (pack_id,))
        if not row:
            return False, False
        await self.conn.execute("UPDATE packs SET tipo = 'pack', criado_em = ? WHERE id = ?", (_agora(), pack_id))
        await self.conn.commit()
        return True, bool(row["postar_canal"])

    async def cancelar_pack_agendado(self, pack_id: str) -> None:
        await self.conn.execute("UPDATE packs SET ativo = 0 WHERE id = ? AND tipo = 'agendado'", (pack_id,))
        await self.conn.commit()

    # ── Enquetes ───────────────────────────────────────────────────────────
    async def votar(self, post_id: int, user_id: int, opcao: int) -> bool:
        """Um voto por fã (não muda depois). Devolve False se já tinha votado."""
        cur = await self.conn.execute("INSERT OR IGNORE INTO votos VALUES (?, ?, ?, ?)", (post_id, user_id, opcao, _agora()))
        await self.conn.commit()
        return cur.rowcount == 1

    async def resultados_enquetes(self, user_id: int) -> dict[int, dict]:
        """{post_id: {"votos": {opcao: n}, "meu": opcao | None}}"""
        saida: dict[int, dict] = {}
        for r in await self._todos("SELECT post_id, opcao, COUNT(*) AS n, MAX(user_id = ?) AS eu FROM votos "
                                   "GROUP BY post_id, opcao", (user_id,)):
            d = saida.setdefault(r["post_id"], {"votos": {}, "meu": None})
            d["votos"][r["opcao"]] = r["n"]
            if r["eu"]:
                d["meu"] = r["opcao"]
        return saida

    # ── Avisos de lançamento ───────────────────────────────────────────────
    async def alternar_aviso(self, pack_id: str, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM avisos_lancamento WHERE pack_id = ? AND user_id = ?", (pack_id, user_id))
        ligado = cur.rowcount == 0
        if ligado:
            await self.conn.execute("INSERT INTO avisos_lancamento VALUES (?, ?, ?)", (pack_id, user_id, _agora()))
        await self.conn.commit()
        return ligado

    async def avisos_do_usuario(self, user_id: int) -> set[str]:
        return {r["pack_id"] for r in await self._todos("SELECT pack_id FROM avisos_lancamento WHERE user_id = ?", (user_id,))}

    async def contar_avisos(self) -> dict[str, int]:
        return {r["pack_id"]: r["n"] for r in await self._todos(
            "SELECT pack_id, COUNT(*) AS n FROM avisos_lancamento GROUP BY pack_id")}

    async def quem_pediu_aviso(self, pack_id: str) -> list[int]:
        rows = await self._todos("SELECT a.user_id FROM avisos_lancamento a LEFT JOIN usuarios u ON u.user_id = a.user_id "
                                 "WHERE a.pack_id = ? AND COALESCE(u.bloqueado, 0) = 0 AND COALESCE(u.banido, 0) = 0",
                                 (pack_id,))
        return [r["user_id"] for r in rows]

    # ── Fila de revisão ────────────────────────────────────────────────────
    async def mandar_post_para_fila(self, post_id: int) -> None:
        await self.conn.execute("UPDATE posts SET status = 'fila' WHERE id = ?", (post_id,))
        await self.conn.commit()

    async def mandar_pack_para_fila(self, pack_id: str, postar_canal: bool) -> None:
        await self.conn.execute("UPDATE packs SET tipo = 'fila', postar_canal = ? WHERE id = ?", (int(postar_canal), pack_id))
        await self.conn.commit()

    async def aprovar_post(self, post_id: int) -> bool:
        cur = await self.conn.execute("UPDATE posts SET status = 'publicado', criado_em = ? WHERE id = ? AND status = 'fila'",
                                      (_agora(), post_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def aprovar_pack(self, pack_id: str) -> bool:
        """Publica o pack da fila. Devolve se ele pediu para ser postado no canal."""
        row = await self._um("SELECT postar_canal FROM packs WHERE id = ? AND tipo = 'fila' AND ativo = 1", (pack_id,))
        if not row:
            return False
        await self.conn.execute("UPDATE packs SET tipo = 'pack', criado_em = ? WHERE id = ?", (_agora(), pack_id))
        await self.conn.commit()
        return bool(row["postar_canal"])

    async def descartar_pack(self, pack_id: str) -> None:
        await self.conn.execute("UPDATE packs SET ativo = 0 WHERE id = ? AND tipo = 'fila'", (pack_id,))
        await self.conn.commit()

    async def apagar_tudo(self) -> list[str]:
        """Zera o banco inteiro (todas as tabelas). Devolve as tabelas apagadas."""
        async with self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                     "AND name NOT LIKE 'sqlite_%'") as cur:
            tabelas = [r["name"] for r in await cur.fetchall()]
        for t in tabelas:
            await self.conn.execute(f'DELETE FROM "{t}"')
        try:
            await self.conn.execute("DELETE FROM sqlite_sequence")
        except Exception:
            pass
        await self.conn.commit()
        await self.conn.execute("VACUUM")
        return tabelas

    async def get_post(self, post_id: int) -> dict | None:
        row = await self._um("SELECT id, texto, foto, video, criado_em FROM posts WHERE id = ? AND ativo = 1", (post_id,))
        return dict(row) if row else None

    async def editar_post(self, post_id: int, texto: str) -> None:
        await self.conn.execute("UPDATE posts SET texto = ? WHERE id = ?", (texto, post_id))
        await self.conn.commit()

    async def apagar_post(self, post_id: int) -> None:
        await self.conn.execute("UPDATE posts SET ativo = 0 WHERE id = ?", (post_id,))
        await self.conn.commit()

    async def editar_pack(self, pack_id: str, titulo: str, preco: int, legenda: str) -> None:
        await self.conn.execute("UPDATE packs SET titulo = ?, preco = ?, legenda = ? WHERE id = ?",
                                (titulo, preco, legenda, pack_id))
        await self.conn.commit()

    async def apagar_pack(self, pack_id: str) -> None:
        """Tira da vitrine e para de vender. Quem já comprou continua com acesso (/meus_packs e galeria)."""
        await self.conn.execute("UPDATE packs SET tipo = 'arquivado' WHERE id = ?", (pack_id,))
        await self.conn.commit()

    # ── Curtidas e mimos do feed ───────────────────────────────────────────
    async def curtir(self, user_id: int, alvo: str) -> tuple[bool, int]:
        """Liga/desliga a curtida. Devolve (curtido agora, total do alvo)."""
        cur = await self.conn.execute("DELETE FROM curtidas WHERE user_id = ? AND alvo = ?", (user_id, alvo))
        curtido = cur.rowcount == 0
        if curtido:
            await self.conn.execute("INSERT INTO curtidas VALUES (?, ?, ?)", (user_id, alvo, _agora()))
        await self.conn.commit()
        total = await self._um("SELECT COUNT(*) AS n FROM curtidas WHERE alvo = ?", (alvo,))
        return curtido, total["n"]

    async def curtidas_do_feed(self, user_id: int) -> dict[str, dict]:
        rows = await self._todos("SELECT alvo, COUNT(*) AS n, MAX(user_id = ?) AS eu FROM curtidas GROUP BY alvo",
                                 (user_id,))
        return {r["alvo"]: {"n": r["n"], "eu": bool(r["eu"])} for r in rows}

    async def mimos_do_feed(self) -> dict[str, dict]:
        """Mimos pagos por post/pack: {alvo: {qtd, total}}."""
        rows = await self._todos(
            "SELECT pack_id, COUNT(*) AS n, SUM(valor) AS total FROM vendas "
            "WHERE status = ? AND pack_id LIKE 'gorjeta\\_%\\_%' ESCAPE '\\' GROUP BY pack_id", (STATUS_PAGO,))
        saida: dict[str, dict] = {}
        for r in rows:
            alvo = r["pack_id"][8:].partition("_")[2]
            d = saida.setdefault(alvo, {"qtd": 0, "total": 0})
            d["qtd"] += r["n"]
            d["total"] += r["total"]
        return saida

    # ── Estatísticas do painel ─────────────────────────────────────────────
    async def vendas_por_dia(self, desde: datetime, fuso_horas: int = -3) -> dict[str, int]:
        """Faturamento por dia (data local AAAA-MM-DD -> centavos)."""
        rows = await self._todos(
            "SELECT date(pago_em, ? || ' hours') AS dia, SUM(valor) AS total FROM vendas "
            "WHERE status = ? AND pago_em >= ? GROUP BY dia", (str(fuso_horas), STATUS_PAGO, iso(desde)))
        return {r["dia"]: r["total"] for r in rows}

    async def receita_por_tipo(self, desde: datetime) -> dict[str, int]:
        rows = await self._todos(
            "SELECT CASE WHEN pack_id LIKE 'plano_%' THEN 'vip' WHEN pack_id LIKE 'gorjeta_%' THEN 'mimos' "
            "ELSE 'packs' END AS tipo, SUM(valor) AS total FROM vendas WHERE status = ? AND pago_em >= ? GROUP BY tipo",
            (STATUS_PAGO, iso(desde)))
        return {r["tipo"]: r["total"] for r in rows}

    async def top_itens(self, desde: datetime, limite: int = 5) -> list[dict]:
        rows = await self._todos(
            "SELECT pack_id, COUNT(*) AS n, SUM(valor) AS total FROM vendas WHERE status = ? AND pago_em >= ? "
            "AND (pack_id LIKE 'pack_%' OR pack_id LIKE 'combo_%') GROUP BY pack_id ORDER BY total DESC LIMIT ?",
            (STATUS_PAGO, iso(desde), limite))
        return [dict(r) for r in rows]

    async def curtidas_desde(self, desde: datetime) -> int:
        row = await self._um("SELECT COUNT(*) AS n FROM curtidas WHERE criado_em >= ?", (iso(desde),))
        return row["n"]

    # ── Vendas ─────────────────────────────────────────────────────────────
    async def criar_venda(self, user_id: int, item_id: str, valor: int,
                          valor_original: int | None = None, cupom: str | None = None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO vendas (user_id, pack_id, status, valor, valor_original, cupom, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, item_id, STATUS_PENDENTE, valor, valor_original or valor, cupom, _agora()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def definir_pix(self, venda_id: int, pix_id: str, copia_e_cola: str = "",
                          expira_em: datetime | None = None) -> None:
        await self.conn.execute("UPDATE vendas SET pix_id = ?, pix_copia_cola = ?, expira_em = ? WHERE id = ?",
                                (pix_id, copia_e_cola, iso(expira_em) if expira_em else None, venda_id))
        await self.conn.commit()

    async def get_venda(self, venda_id: int) -> Venda | None:
        row = await self._um("SELECT * FROM vendas WHERE id = ?", (venda_id,))
        return self._venda(row) if row else None

    async def vendas_vencidas(self) -> list[Venda]:
        rows = await self._todos("SELECT * FROM vendas WHERE status = ? AND expira_em IS NOT NULL AND expira_em <= ?",
                                 (STATUS_PENDENTE, _agora()))
        return [self._venda(r) for r in rows]

    async def marcar_expirada(self, venda_id: int) -> bool:
        cur = await self.conn.execute("UPDATE vendas SET status = ? WHERE id = ? AND status = ?",
                                      (STATUS_EXPIRADO, venda_id, STATUS_PENDENTE))
        await self.conn.commit()
        return cur.rowcount == 1

    async def definir_pix_id(self, venda_id: int, pix_id: str) -> None:   # compatibilidade com o MVP
        await self.definir_pix(venda_id, pix_id)

    async def get_venda_por_pix(self, pix_id: str) -> Venda | None:
        row = await self._um("SELECT * FROM vendas WHERE pix_id = ?", (pix_id,))
        return self._venda(row) if row else None

    async def marcar_pago(self, pix_id: str) -> bool:
        """
        Marca a venda como PAGA de forma atômica e contabiliza o uso do cupom.
        Retorna True só na PRIMEIRA vez (evita entregar o pack duas vezes
        se o gateway reenviar o webhook). Uma venda EXPIRADA também pode ser paga:
        se o dinheiro caiu, o cliente recebe.
        """
        cur = await self.conn.execute(
            "UPDATE vendas SET status = ?, pago_em = ? WHERE pix_id = ? AND status IN (?, ?)",
            (STATUS_PAGO, _agora(), pix_id, STATUS_PENDENTE, STATUS_EXPIRADO),
        )
        if cur.rowcount == 1:
            await self.conn.execute(
                "UPDATE cupons SET usos = usos + 1 WHERE codigo = (SELECT cupom FROM vendas WHERE pix_id = ?)",
                (pix_id,),
            )
        await self.conn.commit()
        return cur.rowcount == 1

    async def liberar_acesso(self, user_id: int, pack_ids: list[str], venda_id: int | None) -> None:
        await self.conn.executemany(
            "INSERT OR IGNORE INTO acessos (user_id, pack_id, venda_id, criado_em) VALUES (?, ?, ?, ?)",
            [(user_id, p, venda_id, _agora()) for p in pack_ids],
        )
        await self.conn.commit()

    async def ja_comprou(self, user_id: int, pack_id: str) -> bool:
        return await self._um("SELECT 1 FROM acessos WHERE user_id = ? AND pack_id = ?", (user_id, pack_id)) is not None

    async def tem_todos(self, user_id: int, pack_ids: list[str]) -> bool:
        if not pack_ids:
            return False
        marcas = ",".join("?" * len(pack_ids))
        row = await self._um(f"SELECT COUNT(*) AS n FROM acessos WHERE user_id = ? AND pack_id IN ({marcas})",
                             (user_id, *pack_ids))
        return row["n"] == len(set(pack_ids))

    async def packs_do_cliente(self, user_id: int) -> list[Pack]:
        rows = await self._todos("SELECT pack_id FROM acessos WHERE user_id = ? ORDER BY criado_em", (user_id,))
        return [p for p in [await self.get_pack(r["pack_id"]) for r in rows] if p]

    async def vendas_para_lembrete(self, criadas_antes_de: datetime) -> list[Venda]:
        """PIX ainda válido, sem lembrete enviado."""
        rows = await self._todos(
            "SELECT * FROM vendas WHERE status = ? AND remarketing_etapa = 0 AND criado_em <= ? "
            "AND pix_id IS NOT NULL AND (expira_em IS NULL OR expira_em > ?) ORDER BY id",
            (STATUS_PENDENTE, iso(criadas_antes_de), _agora()),
        )
        return [self._venda(r) for r in rows]

    async def vendas_para_cupom(self, criadas_antes_de: datetime) -> list[Venda]:
        """Não pagas (pendentes ou já expiradas) que ainda não ganharam cupom."""
        rows = await self._todos(
            "SELECT * FROM vendas WHERE status IN (?, ?) AND remarketing_etapa < 2 AND criado_em <= ? "
            "AND pix_id IS NOT NULL ORDER BY id",
            (STATUS_PENDENTE, STATUS_EXPIRADO, iso(criadas_antes_de)),
        )
        return [self._venda(r) for r in rows]

    async def definir_etapa_remarketing(self, venda_id: int, etapa: int) -> None:
        await self.conn.execute("UPDATE vendas SET remarketing_etapa = ? WHERE id = ?", (etapa, venda_id))
        await self.conn.commit()

    async def tem_venda_mais_nova(self, user_id: int, item_id: str, venda_id: int) -> bool:
        """O cliente gerou outro PIX (ou pagou) este item depois? Aí só a venda mais nova recebe remarketing."""
        return await self._um(
            "SELECT 1 FROM vendas WHERE user_id = ? AND pack_id = ? AND id > ?",
            (user_id, item_id, venda_id)) is not None

    @staticmethod
    def _venda(row) -> Venda:
        chaves = row.keys()
        return Venda(
            id=row["id"], user_id=row["user_id"], pack_id=row["pack_id"],
            status=row["status"], pix_id=row["pix_id"], valor=row["valor"],
            valor_original=row["valor_original"] if "valor_original" in chaves else None,
            cupom=row["cupom"] if "cupom" in chaves else None,
            pix_copia_cola=row["pix_copia_cola"] if "pix_copia_cola" in chaves else None,
            remarketing_etapa=row["remarketing_etapa"] if "remarketing_etapa" in chaves else 0,
            criado_em=row["criado_em"],
            expira_em=row["expira_em"] if "expira_em" in chaves else None,
        )

    # ── Cupons ─────────────────────────────────────────────────────────────
    async def criar_cupom(self, codigo: str, tipo: str, valor: int, item_id: str | None = None,
                          user_id: int | None = None, usos_max: int | None = None,
                          expira_em: datetime | None = None) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO cupons (codigo, tipo, valor, item_id, user_id, usos_max, usos, expira_em, ativo, criado_em) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, 1, ?)",
            (codigo.upper(), tipo, valor, item_id, user_id, usos_max,
             iso(expira_em) if expira_em else None, _agora()),
        )
        await self.conn.commit()

    async def get_cupom(self, codigo: str) -> Cupom | None:
        row = await self._um("SELECT * FROM cupons WHERE codigo = ?", (codigo.upper(),))
        if not row:
            return None
        return Cupom(codigo=row["codigo"], tipo=row["tipo"], valor=row["valor"], item_id=row["item_id"],
                     user_id=row["user_id"], usos_max=row["usos_max"], usos=row["usos"],
                     expira_em=row["expira_em"], ativo=bool(row["ativo"]))

    async def validar_cupom(self, codigo: str, item_id: str, user_id: int) -> tuple[Cupom | None, str]:
        """Retorna (cupom, '') se puder usar, ou (None, motivo)."""
        c = await self.get_cupom(codigo)
        if not c or not c.ativo:
            return None, "cupom não encontrado"
        if c.expira_em and c.expira_em < _agora():
            return None, "cupom expirado"
        if c.usos_max is not None and c.usos >= c.usos_max:
            return None, "cupom esgotado"
        if c.item_id and c.item_id != item_id:
            return None, "cupom não vale para este item"
        if c.user_id and c.user_id != user_id:
            return None, "cupom pessoal de outro cliente"
        return c, ""

    async def listar_cupons(self, incluir_pessoais: bool = False) -> list[Cupom]:
        sql = "SELECT codigo FROM cupons WHERE ativo = 1" + ("" if incluir_pessoais else " AND user_id IS NULL")
        rows = await self._todos(sql + " ORDER BY criado_em DESC")
        return [c for c in [await self.get_cupom(r["codigo"]) for r in rows] if c]

    async def desativar_cupom(self, codigo: str) -> bool:
        cur = await self.conn.execute("UPDATE cupons SET ativo = 0 WHERE codigo = ?", (codigo.upper(),))
        await self.conn.commit()
        return cur.rowcount == 1

    # ── Usuários e cliques ─────────────────────────────────────────────────
    async def registrar_usuario(self, user_id: int, nome: str, username: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO usuarios (user_id, nome, username, criado_em) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET nome = excluded.nome, username = excluded.username, bloqueado = 0",
            (user_id, nome, username, _agora()),
        )
        await self.conn.commit()

    async def status_idade(self, user_id: int) -> str | None:
        """'ok' (confirmou 18+), 'menor' (declarou ser menor) ou None (ainda não respondeu)."""
        row = await self._um("SELECT maior_idade_em, menor FROM usuarios WHERE user_id = ?", (user_id,))
        if not row:
            return None
        if row["menor"]:
            return "menor"
        return "ok" if row["maior_idade_em"] else None

    async def registrar_idade(self, user_id: int, maior: bool) -> None:
        await self.conn.execute(
            "INSERT INTO usuarios (user_id, criado_em) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING",
            (user_id, _agora()))
        if maior:
            await self.conn.execute("UPDATE usuarios SET maior_idade_em = ?, menor = 0 WHERE user_id = ?",
                                    (_agora(), user_id))
        else:
            await self.conn.execute("UPDATE usuarios SET menor = 1, maior_idade_em = NULL WHERE user_id = ?",
                                    (user_id,))
        await self.conn.commit()

    async def backup(self, destino: str) -> None:
        """Cópia consistente do banco (API de backup do SQLite), mesmo com o bot rodando."""
        import aiosqlite as _a
        alvo = await _a.connect(destino)
        try:
            await self.conn.backup(alvo)
        finally:
            await alvo.close()

    # ── Fãs bloqueados pela criadora ───────────────────────────────────────
    async def banir(self, user_id: int, banido: bool = True) -> None:
        await self.conn.execute(
            "INSERT INTO usuarios (user_id, criado_em) VALUES (?, ?) ON CONFLICT(user_id) DO NOTHING", (user_id, _agora()))
        await self.conn.execute("UPDATE usuarios SET banido = ?, banido_em = ? WHERE user_id = ?",
                                (int(banido), _agora() if banido else None, user_id))
        await self.conn.commit()

    async def esta_banido(self, user_id: int) -> bool:
        return await self._um("SELECT 1 FROM usuarios WHERE user_id = ? AND banido = 1", (user_id,)) is not None

    async def listar_banidos(self) -> list[dict]:
        rows = await self._todos("SELECT user_id, nome, username, banido_em FROM usuarios WHERE banido = 1 "
                                 "ORDER BY banido_em DESC")
        return [dict(r) for r in rows]

    async def get_usuario(self, user_id: int) -> dict | None:
        row = await self._um("SELECT user_id, nome, username, criado_em, banido FROM usuarios WHERE user_id = ?", (user_id,))
        return dict(row) if row else None

    async def total_gasto(self, user_id: int) -> dict:
        row = await self._um("SELECT COUNT(*) AS n, COALESCE(SUM(valor), 0) AS total FROM vendas "
                             "WHERE user_id = ? AND status = ?", (user_id, STATUS_PAGO))
        return {"compras": row["n"], "total": row["total"]}

    # ── Limite de reenvios no chat ─────────────────────────────────────────
    async def reenvios_feitos(self, user_id: int, pack_ids: list[str]) -> int:
        """O maior número de reenvios entre os packs (combo conta pelo pack mais reenviado)."""
        if not pack_ids:
            return 0
        marcas = ",".join("?" * len(pack_ids))
        row = await self._um(f"SELECT MAX(n) AS n FROM reenvios WHERE user_id = ? AND pack_id IN ({marcas})",
                             (user_id, *pack_ids))
        return (row["n"] if row else None) or 0

    async def registrar_reenvio(self, user_id: int, pack_ids: list[str]) -> None:
        await self.conn.executemany(
            "INSERT INTO reenvios (user_id, pack_id, n) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, pack_id) DO UPDATE SET n = n + 1", [(user_id, p) for p in pack_ids])
        await self.conn.commit()

    async def marcar_bloqueado(self, user_id: int) -> None:
        await self.conn.execute(
            "INSERT INTO usuarios (user_id, bloqueado, criado_em) VALUES (?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET bloqueado = 1", (user_id, _agora()))
        await self.conn.commit()

    async def registrar_clique(self, user_id: int, item_id: str) -> None:
        await self.conn.execute("INSERT INTO cliques (user_id, item_id, criado_em) VALUES (?, ?, ?)",
                                (user_id, item_id, _agora()))
        await self.conn.commit()

    async def todos_clientes(self) -> list[int]:
        """Todo mundo que já falou com o bot e não o bloqueou."""
        rows = await self._todos("SELECT user_id FROM usuarios WHERE bloqueado = 0 AND menor = 0 AND banido = 0 ORDER BY user_id")
        return [r["user_id"] for r in rows]

    async def compradores(self, item_id: str | None = None) -> list[int]:
        """IDs de quem já comprou (opcionalmente só de um pack), sem quem bloqueou o bot."""
        sql = ("SELECT DISTINCT a.user_id FROM acessos a LEFT JOIN usuarios u ON u.user_id = a.user_id "
               "WHERE COALESCE(u.bloqueado, 0) = 0 AND COALESCE(u.banido, 0) = 0")
        params: tuple = ()
        if item_id:
            sql += " AND a.pack_id = ?"
            params = (item_id,)
        return [r["user_id"] for r in await self._todos(sql + " ORDER BY a.user_id", params)]

    # ── Estatísticas ───────────────────────────────────────────────────────
    async def stats_periodo(self, desde: datetime) -> dict:
        d = iso(desde)
        pagas = await self._um(
            "SELECT COUNT(*) AS n, COALESCE(SUM(valor), 0) AS total FROM vendas WHERE status = ? AND pago_em >= ?",
            (STATUS_PAGO, d))
        geradas = await self._um("SELECT COUNT(*) AS n FROM vendas WHERE criado_em >= ?", (d,))
        cliques = await self._um(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT user_id, item_id FROM cliques WHERE criado_em >= ?)", (d,))
        compradores = await self._um(
            "SELECT COUNT(*) AS n FROM (SELECT DISTINCT user_id, pack_id FROM vendas WHERE status = ? AND pago_em >= ?)",
            (STATUS_PAGO, d))
        novos = await self._um("SELECT COUNT(*) AS n FROM usuarios WHERE criado_em >= ?", (d,))
        top = await self._um(
            "SELECT pack_id, COUNT(*) AS n, SUM(valor) AS total FROM vendas WHERE status = ? AND pago_em >= ? "
            "GROUP BY pack_id ORDER BY n DESC, total DESC LIMIT 1", (STATUS_PAGO, d))
        com_cupom = await self._um(
            "SELECT COUNT(*) AS n FROM vendas WHERE status = ? AND pago_em >= ? AND cupom IS NOT NULL",
            (STATUS_PAGO, d))
        return {
            "vendas": pagas["n"],
            "faturamento": pagas["total"],
            "ticket_medio": pagas["total"] // pagas["n"] if pagas["n"] else 0,
            "pix_gerados": geradas["n"],
            "cliques": cliques["n"],
            "compras_unicas": compradores["n"],
            "conversao": (compradores["n"] / cliques["n"]) if cliques["n"] else None,
            "novos_usuarios": novos["n"],
            "top_item": top["pack_id"] if top else None,
            "top_qtd": top["n"] if top else 0,
            "com_cupom": com_cupom["n"],
        }

    async def pendentes_abertos(self) -> int:
        row = await self._um("SELECT COUNT(*) AS n FROM vendas WHERE status = ? AND criado_em >= ?",
                             (STATUS_PENDENTE, iso(agora() - timedelta(days=1))))
        return row["n"]
