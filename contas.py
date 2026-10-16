"""
Contas da criadora e da equipe: cadastro por convite, login com e-mail e senha, vínculo com o Telegram.

Fluxo:
  1. O dono da plataforma (DONO_IDS, padrão = RESET_IDS) manda /convite no bot e recebe um link de cadastro.
  2. A criadora abre o link (PUBLIC_BASE_URL/painel/cadastro?c=CÓDIGO), cria a conta com nome, e-mail e senha.
  3. Ela vincula o Telegram tocando no botão (abre o bot com /start vc_...). A partir daí vira admin do bot,
     abre a loja no modo criadora e também entra pelo navegador (PUBLIC_BASE_URL/painel) com e-mail e senha.
  4. A criadora convida a equipe em Ajustes › Equipe, escolhendo o que cada pessoa pode fazer.

Quem está em ADMIN_IDS continua admin como antes (as contas só somam).

Segurança:
  - Senha com scrypt (salt por conta). Login com limite de tentativas por IP e por e-mail.
  - Sessão num cookie HttpOnly assinado (HMAC com o token do bot). O servidor guarda a "versão" da sessão
    de cada conta: trocar a senha ou "sair de todos os aparelhos" invalida os cookies antigos na hora.
  - O vínculo com o Telegram e a troca de senha usam links assinados que vencem.
  - Requisições POST da API com cookie exigem o cabeçalho X-Requested-With (barra formulários de outros sites).
"""
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time
from datetime import timedelta
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, BotCommandScopeChat, Message
from aiohttp import web

from config import Config
from database import Database, _agora, agora, iso

log = logging.getLogger(__name__)
PAGINA = Path(__file__).parent / "miniapp" / "painel.html"
COOKIE = "sessao"
VALIDADE_SESSAO = 30 * 24 * 3600
VALIDADE_CONVITE = 7 * 24 * 3600
VALIDADE_VINCULO = 30 * 60
VALIDADE_REDEFINIR = 30 * 60
RE_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.I)

# O que um membro da equipe pode fazer (a dona pode tudo)
PERMISSOES = {
    "publicar": "Publicar posts, packs e stories",
    "pedidos": "Entregar pedidos e responder perguntas",
    "fas": "Ver fãs, moderar comentários e bloquear",
    "financeiro": "Ver vendas, preços, planos, cupons e relatórios",
    "ajustes": "Mudar configurações e recursos",
}
TODAS = list(PERMISSOES)
EQUIPE: dict[int, list[str]] = {}                 # telegram_id -> permissões (atualizado no lugar)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT    NOT NULL UNIQUE,         -- sempre minúsculo
    senha         TEXT    NOT NULL,                -- scrypt$salt$hash
    nome          TEXT    NOT NULL,
    telegram_id   INTEGER UNIQUE,                  -- NULL até vincular
    papel         TEXT    NOT NULL DEFAULT 'dona', -- dona | equipe
    permissoes    TEXT    NOT NULL DEFAULT '[]',   -- JSON (equipe)
    versao        INTEGER NOT NULL DEFAULT 1,      -- muda ao trocar a senha: derruba as sessões antigas
    ativo         INTEGER NOT NULL DEFAULT 1,
    criado_em     TEXT    NOT NULL,
    ultimo_login  TEXT
);
CREATE TABLE IF NOT EXISTS convites (
    codigo        TEXT PRIMARY KEY,
    papel         TEXT    NOT NULL,
    permissoes    TEXT    NOT NULL DEFAULT '[]',
    nome          TEXT,                            -- para quem é (só para lembrar)
    criado_por    INTEGER,                         -- telegram_id de quem gerou
    usado_por     INTEGER,                         -- conta criada
    criado_em     TEXT    NOT NULL,
    expira_em     TEXT    NOT NULL
);
"""


# ════════════════════════════════════════════════════════════════════════════
#  Senhas e assinaturas
# ════════════════════════════════════════════════════════════════════════════
def hash_senha(senha: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(senha.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def conferir_senha(senha: str, guardada: str) -> bool:
    try:
        _, salt, h = guardada.split("$")
        calc = hashlib.scrypt(senha.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def _chave(cfg: Config, uso: str) -> bytes:
    return hashlib.sha256(f"contas:{uso}:".encode() + cfg.bot_token.encode()).digest()


def _assinar(cfg: Config, uso: str, texto: str, tamanho: int = 32) -> str:
    return hmac.new(_chave(cfg, uso), texto.encode(), hashlib.sha256).hexdigest()[:tamanho]


def token_sessao(cfg: Config, conta_id: int, versao: int) -> str:
    exp = int(time.time()) + VALIDADE_SESSAO
    corpo = f"{conta_id}.{versao}.{exp}"
    return f"{corpo}.{_assinar(cfg, 'sessao', corpo)}"


def ler_sessao(cfg: Config, token: str) -> tuple[int, int] | None:
    """(conta_id, versao) se o cookie for válido e não venceu."""
    try:
        conta_id, versao, exp, sig = token.split(".")
        if int(exp) < time.time():
            return None
        if not hmac.compare_digest(sig, _assinar(cfg, "sessao", f"{conta_id}.{versao}.{exp}")):
            return None
        return int(conta_id), int(versao)
    except (ValueError, AttributeError):
        return None


def payload_vinculo(cfg: Config, conta_id: int) -> str:
    """Parâmetro do /start (máx. 64 caracteres, só A-Z a-z 0-9 _ -)."""
    exp = int(time.time()) + VALIDADE_VINCULO
    return f"vc_{conta_id}_{exp}_{_assinar(cfg, 'vinculo', f'{conta_id}:{exp}', 20)}"


def ler_vinculo(cfg: Config, payload: str) -> int | None:
    m = re.fullmatch(r"vc_(\d+)_(\d+)_([0-9a-f]{20})", payload or "")
    if not m or int(m[2]) < time.time():
        return None
    if not hmac.compare_digest(m[3], _assinar(cfg, "vinculo", f"{m[1]}:{m[2]}", 20)):
        return None
    return int(m[1])


def token_redefinir(cfg: Config, conta: dict) -> str:
    exp = int(time.time()) + VALIDADE_REDEFINIR
    # inclui um pedaço do hash atual: depois que a senha muda, o link para de valer (uso único)
    corpo = f"{conta['id']}.{exp}"
    return f"{corpo}.{_assinar(cfg, 'redefinir', corpo + conta['senha'][-16:])}"


# ════════════════════════════════════════════════════════════════════════════
#  Banco e cache em memória (o _usuario da loja é síncrono)
# ════════════════════════════════════════════════════════════════════════════
class Contas:
    def __init__(self, db: Database, cfg: Config):
        self.db, self.cfg = db, cfg
        self.base_admins = set(cfg.admin_ids)          # ADMIN_IDS do ambiente (nunca são removidos)
        self.cache: dict[int, dict] = {}               # conta_id -> conta
        self.equipe = EQUIPE

    async def iniciar(self) -> None:
        await self.db.conn.executescript(SCHEMA)
        await self.db.conn.commit()
        await self.recarregar()

    async def recarregar(self) -> None:
        rows = await self.db._todos("SELECT * FROM contas")
        self.cache = {r["id"]: self._conta(r) for r in rows}
        equipe, donas = {}, set()
        for c in self.cache.values():
            if not c["ativo"] or not c["telegram_id"]:
                continue
            if c["papel"] == "dona":
                donas.add(c["telegram_id"])
            else:
                equipe[c["telegram_id"]] = c["permissoes"]
        EQUIPE.clear()
        EQUIPE.update(equipe)
        # admin_ids é o conjunto usado por todo o bot: ADMIN_IDS do ambiente + donas vinculadas
        self.cfg.admin_ids.clear()
        self.cfg.admin_ids.update(self.base_admins | donas)

    @staticmethod
    def _conta(r) -> dict:
        c = dict(r)
        c["permissoes"] = TODAS if c["papel"] == "dona" else [p for p in json.loads(c["permissoes"] or "[]") if p in PERMISSOES]
        return c

    def permissoes(self, telegram_id: int) -> list[str] | None:
        """Todas para a dona/admin, a lista da equipe, ou None para quem não é da casa."""
        if telegram_id in self.cfg.admin_ids:
            return TODAS
        return self.equipe.get(telegram_id)

    def conta_por_telegram(self, telegram_id: int) -> dict | None:
        return next((c for c in self.cache.values() if c["telegram_id"] == telegram_id), None)

    def conta_da_sessao(self, token: str | None) -> dict | None:
        lido = ler_sessao(self.cfg, token or "")
        if not lido:
            return None
        c = self.cache.get(lido[0])
        if not c or not c["ativo"] or c["versao"] != lido[1]:
            return None
        return c

    async def por_email(self, email: str) -> dict | None:
        r = await self.db._um("SELECT * FROM contas WHERE email = ?", (email.strip().lower(),))
        return self._conta(r) if r else None

    async def criar_convite(self, papel: str, permissoes: list[str], criado_por: int | None, nome: str = "") -> str:
        codigo = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
        await self.db.conn.execute(
            "INSERT INTO convites (codigo, papel, permissoes, nome, criado_por, criado_em, expira_em) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (codigo, papel, json.dumps(permissoes), nome[:60], criado_por, _agora(),
             iso(agora() + timedelta(seconds=VALIDADE_CONVITE))))
        await self.db.conn.commit()
        return codigo

    async def convite(self, codigo: str) -> dict | None:
        r = await self.db._um("SELECT * FROM convites WHERE codigo = ? AND usado_por IS NULL AND expira_em > ?",
                              (codigo, _agora()))
        return dict(r) if r else None

    async def convites_abertos(self) -> list[dict]:
        rows = await self.db._todos("SELECT * FROM convites WHERE usado_por IS NULL AND expira_em > ? AND papel = 'equipe' "
                                    "ORDER BY criado_em DESC", (_agora(),))
        return [dict(r) for r in rows]

    async def cancelar_convite(self, codigo: str) -> None:
        await self.db.conn.execute("DELETE FROM convites WHERE codigo = ? AND usado_por IS NULL", (codigo,))
        await self.db.conn.commit()

    async def cadastrar(self, convite: dict, nome: str, email: str, senha: str) -> dict:
        cur = await self.db.conn.execute(
            "INSERT INTO contas (email, senha, nome, papel, permissoes, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
            (email.strip().lower(), hash_senha(senha), nome.strip()[:60], convite["papel"], convite["permissoes"], _agora()))
        await self.db.conn.execute("UPDATE convites SET usado_por = ? WHERE codigo = ?", (cur.lastrowid, convite["codigo"]))
        await self.db.conn.commit()
        await self.recarregar()
        return self.cache[cur.lastrowid]

    async def vincular(self, conta_id: int, telegram_id: int) -> str:
        """'ok' | 'outra_conta' (esse Telegram já está em outra conta) | 'inexistente'"""
        c = self.cache.get(conta_id)
        if not c or not c["ativo"]:
            return "inexistente"
        dono = self.conta_por_telegram(telegram_id)
        if dono and dono["id"] != conta_id:
            return "outra_conta"
        await self.db.conn.execute("UPDATE contas SET telegram_id = ? WHERE id = ?", (telegram_id, conta_id))
        await self.db.conn.commit()
        await self.recarregar()
        return "ok"

    async def trocar_senha(self, conta_id: int, senha: str) -> dict:
        await self.db.conn.execute("UPDATE contas SET senha = ?, versao = versao + 1 WHERE id = ?",
                                   (hash_senha(senha), conta_id))
        await self.db.conn.commit()
        await self.recarregar()
        return self.cache[conta_id]

    async def sair_de_todos(self, conta_id: int) -> dict:
        await self.db.conn.execute("UPDATE contas SET versao = versao + 1 WHERE id = ?", (conta_id,))
        await self.db.conn.commit()
        await self.recarregar()
        return self.cache[conta_id]

    async def marcar_login(self, conta_id: int) -> None:
        await self.db.conn.execute("UPDATE contas SET ultimo_login = ? WHERE id = ?", (_agora(), conta_id))
        await self.db.conn.commit()

    async def salvar_membro(self, conta_id: int, permissoes: list[str] | None = None, ativo: bool | None = None) -> bool:
        c = self.cache.get(conta_id)
        if not c or c["papel"] != "equipe":
            return False
        if permissoes is not None:
            await self.db.conn.execute("UPDATE contas SET permissoes = ? WHERE id = ?",
                                       (json.dumps([p for p in permissoes if p in PERMISSOES]), conta_id))
        if ativo is not None:
            await self.db.conn.execute("UPDATE contas SET ativo = ?, versao = versao + 1 WHERE id = ?", (int(ativo), conta_id))
        await self.db.conn.commit()
        await self.recarregar()
        return True


def contas_de(app: web.Application) -> Contas:
    return app["contas"]


def publico(c: dict) -> dict:
    return {"id": c["id"], "nome": c["nome"], "email": c["email"], "papel": c["papel"],
            "permissoes": c["permissoes"], "vinculado": bool(c["telegram_id"]), "ativo": bool(c["ativo"]),
            "ultimo_login": c["ultimo_login"]}


# ════════════════════════════════════════════════════════════════════════════
#  Limite de tentativas (memória)
# ════════════════════════════════════════════════════════════════════════════
_tentativas: dict[str, list[float]] = {}


def _bloqueado(*chaves: str, maximo: int = 8, janela: int = 15 * 60) -> bool:
    agora_ = time.time()
    for k in chaves:
        lista = [t for t in _tentativas.get(k, []) if agora_ - t < janela]
        _tentativas[k] = lista
        if len(lista) >= maximo:
            return True
    return False


def _falhou(*chaves: str) -> None:
    for k in chaves:
        _tentativas.setdefault(k, []).append(time.time())


def _ip(request: web.Request) -> str:
    return (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote or "?")


# ════════════════════════════════════════════════════════════════════════════
#  Rotas
# ════════════════════════════════════════════════════════════════════════════
def _ok(resp_extra: dict | None = None, **dados) -> web.Response:
    return web.json_response({"ok": True, **dados, **(resp_extra or {})})


def _erro(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "erro": msg}, status=status)


def _seguro(request: web.Request) -> bool:
    cfg: Config = request.app["cfg"]
    return cfg.public_base_url.startswith("https://") or request.headers.get("X-Forwarded-Proto") == "https"


def _com_cookie(resp: web.Response, request: web.Request, conta: dict) -> web.Response:
    resp.set_cookie(COOKIE, token_sessao(request.app["cfg"], conta["id"], conta["versao"]), max_age=VALIDADE_SESSAO,
                    httponly=True, secure=_seguro(request), samesite="Lax", path="/")
    return resp


def conta_logada(request: web.Request) -> dict | None:
    return contas_de(request.app).conta_da_sessao(request.cookies.get(COOKIE))


def _validar_senha(senha: str) -> str | None:
    if len(senha) < 8:
        return "A senha precisa de pelo menos 8 caracteres."
    if len(senha) > 200:
        return "Senha longa demais."
    if senha.isdigit() or senha.lower() == senha and senha.isalpha() and len(set(senha)) < 4:
        return "Escolha uma senha mais forte (misture letras e números)."
    return None


async def pagina(_: web.Request) -> web.FileResponse:
    return web.FileResponse(PAGINA, headers={"Cache-Control": "no-cache"})


async def info_convite(request: web.Request) -> web.Response:
    c = await contas_de(request.app).convite(request.match_info["codigo"])
    if not c:
        return _erro("Esse convite não vale mais. Peça um novo.", 404)
    cfg: Config = request.app["cfg"]
    return _ok(papel=c["papel"], nome=c["nome"] or "", criadora=cfg.criadora_nome,
               permissoes=[PERMISSOES[p] for p in json.loads(c["permissoes"] or "[]") if p in PERMISSOES])


async def cadastrar(request: web.Request) -> web.Response:
    corpo = await request.json()
    contas = contas_de(request.app)
    if _bloqueado(f"cad:{_ip(request)}", maximo=10, janela=3600):
        return _erro("Muitas tentativas. Espere um pouco.", 429)
    convite = await contas.convite(str(corpo.get("convite", "")))
    if not convite:
        _falhou(f"cad:{_ip(request)}")
        return _erro("Esse convite não vale mais. Peça um novo.", 404)
    nome = " ".join(str(corpo.get("nome", "")).split())[:60]
    email = str(corpo.get("email", "")).strip().lower()
    senha = str(corpo.get("senha", ""))
    if len(nome) < 2:
        return _erro("Informe seu nome.")
    if not RE_EMAIL.match(email):
        return _erro("E-mail inválido.")
    if erro := _validar_senha(senha):
        return _erro(erro)
    if await contas.por_email(email):
        return _erro("Já existe uma conta com esse e-mail. Entre com ela.", 409)
    conta = await contas.cadastrar(convite, nome, email, senha)
    log.info("Conta %s criada (%s) pelo convite", conta["id"], conta["papel"])
    await contas.marcar_login(conta["id"])
    return _com_cookie(_ok(conta=publico(conta)), request, conta)


async def entrar(request: web.Request) -> web.Response:
    corpo = await request.json()
    contas = contas_de(request.app)
    email = str(corpo.get("email", "")).strip().lower()
    chaves = (f"ip:{_ip(request)}", f"email:{email}")
    if _bloqueado(*chaves):
        return _erro("Muitas tentativas erradas. Espere 15 minutos ou redefina a senha.", 429)
    conta = await contas.por_email(email)
    if not conta or not conferir_senha(str(corpo.get("senha", "")), conta["senha"]) or not conta["ativo"]:
        _falhou(*chaves)
        return _erro("E-mail ou senha incorretos.", 401)
    _tentativas.pop(f"email:{email}", None)
    await contas.marcar_login(conta["id"])
    return _com_cookie(_ok(conta=publico(contas.cache[conta["id"]])), request, contas.cache[conta["id"]])


async def sair(request: web.Request) -> web.Response:
    resp = _ok()
    resp.del_cookie(COOKIE, path="/")
    return resp


async def eu(request: web.Request) -> web.Response:
    conta = conta_logada(request)
    if not conta:
        return _erro("nao_logado", 401)
    cfg: Config = request.app["cfg"]
    bot: Bot = request.app["bot"]
    extra = {}
    if not conta["telegram_id"]:
        me = await bot.me()
        extra["link_vinculo"] = f"https://t.me/{me.username}?start={payload_vinculo(cfg, conta['id'])}"
    return _ok(conta=publico(conta), criadora=cfg.criadora_nome, **extra)


async def esqueci(request: web.Request) -> web.Response:
    """Manda o link de nova senha no Telegram vinculado (sem servidor de e-mail)."""
    corpo = await request.json()
    contas = contas_de(request.app)
    if _bloqueado(f"esq:{_ip(request)}", maximo=5, janela=3600):
        return _erro("Muitas tentativas. Espere um pouco.", 429)
    _falhou(f"esq:{_ip(request)}")
    conta = await contas.por_email(str(corpo.get("email", "")))
    # resposta igual exista ou não a conta (não revela quais e-mails estão cadastrados)
    if conta and conta["telegram_id"] and conta["ativo"]:
        cfg: Config = request.app["cfg"]
        link = f"{cfg.public_base_url.rstrip('/')}/painel/redefinir?t={token_redefinir(cfg, conta)}"
        try:
            await request.app["bot"].send_message(
                conta["telegram_id"], "🔑 <b>Pedido para trocar a senha do painel</b>\n"
                f"Toque para criar uma senha nova (vale 30 minutos):\n{html.escape(link)}\n\n"
                "Se não foi você, ignore esta mensagem.", disable_web_page_preview=True)
        except Exception:
            log.warning("Não consegui mandar o link de nova senha para a conta %s", conta["id"])
    return _ok(mensagem="Se o e-mail tiver conta com Telegram vinculado, o link chegou no chat com o bot.")


async def redefinir(request: web.Request) -> web.Response:
    corpo = await request.json()
    contas = contas_de(request.app)
    cfg: Config = request.app["cfg"]
    try:
        conta_id, exp, sig = str(corpo.get("token", "")).split(".")
        conta = contas.cache.get(int(conta_id))
        valido = (conta and int(exp) > time.time() and
                  hmac.compare_digest(sig, _assinar(cfg, "redefinir", f"{conta_id}.{exp}" + conta["senha"][-16:])))
    except ValueError:
        valido, conta = False, None
    if not valido:
        return _erro("Esse link venceu ou já foi usado. Peça outro.", 400)
    senha = str(corpo.get("senha", ""))
    if erro := _validar_senha(senha):
        return _erro(erro)
    conta = await contas.trocar_senha(conta["id"], senha)
    return _com_cookie(_ok(conta=publico(conta)), request, conta)


async def mudar_senha(request: web.Request) -> web.Response:
    """Pelo Mini App (Telegram) ou logada no navegador."""
    conta = await _conta_do_pedido(request)
    if not conta:
        return _erro("Você não tem conta de acesso.", 403)
    corpo = await request.json()
    if not conferir_senha(str(corpo.get("atual", "")), conta["senha"]):
        return _erro("A senha atual não confere.", 401)
    if erro := _validar_senha(str(corpo.get("nova", ""))):
        return _erro(erro)
    conta = await contas_de(request.app).trocar_senha(conta["id"], str(corpo["nova"]))
    return _com_cookie(_ok(), request, conta)


async def sair_todos(request: web.Request) -> web.Response:
    conta = await _conta_do_pedido(request)
    if not conta:
        return _erro("Você não tem conta de acesso.", 403)
    conta = await contas_de(request.app).sair_de_todos(conta["id"])
    return _com_cookie(_ok(), request, conta)      # este aparelho continua logado


async def minha(request: web.Request) -> web.Response:
    """Conta de acesso de quem abriu a loja (Telegram ou navegador)."""
    conta = await _conta_do_pedido(request)
    cfg: Config = request.app["cfg"]
    return _ok(conta=publico(conta) if conta else None, painel_url=f"{cfg.public_base_url.rstrip('/')}/painel")


async def _conta_do_pedido(request: web.Request) -> dict | None:
    from loja import _usuario
    user = _usuario(request)
    return contas_de(request.app).conta_por_telegram(user["id"])


# ── Equipe (só a dona) ──────────────────────────────────────────────────────
def _dona(request: web.Request) -> dict:
    from loja import _usuario
    user = _usuario(request)
    if user["id"] not in request.app["cfg"].admin_ids:
        raise web.HTTPForbidden(text='{"erro":"so_criadora"}', content_type="application/json")
    return user


async def acesso(request: web.Request) -> web.Response:
    """Ajustes › Conta e equipe."""
    user = _dona(request)
    contas = contas_de(request.app)
    cfg: Config = request.app["cfg"]
    base = cfg.public_base_url.rstrip("/")
    minha = contas.conta_por_telegram(user["id"])
    return _ok(
        minha=publico(minha) if minha else None,
        painel_url=f"{base}/painel",
        permissoes=[{"id": k, "nome": v} for k, v in PERMISSOES.items()],
        equipe=[publico(c) for c in contas.cache.values() if c["papel"] == "equipe"],
        convites=[{"codigo": c["codigo"], "nome": c["nome"] or "", "link": f"{base}/painel/cadastro?c={c['codigo']}",
                   "permissoes": json.loads(c["permissoes"] or "[]"), "expira_em": c["expira_em"]}
                  for c in await contas.convites_abertos()],
    )


async def convidar(request: web.Request) -> web.Response:
    user = _dona(request)
    corpo = await request.json()
    perms = [p for p in corpo.get("permissoes", []) if p in PERMISSOES]
    if not perms:
        return _erro("Escolha pelo menos uma permissão.")
    codigo = await contas_de(request.app).criar_convite("equipe", perms, user["id"], str(corpo.get("nome", "")).strip())
    cfg: Config = request.app["cfg"]
    return _ok(link=f"{cfg.public_base_url.rstrip('/')}/painel/cadastro?c={codigo}")


async def cancelar_convite(request: web.Request) -> web.Response:
    _dona(request)
    await contas_de(request.app).cancelar_convite(request.match_info["codigo"])
    return _ok()


async def membro(request: web.Request) -> web.Response:
    _dona(request)
    corpo = await request.json()
    perms = corpo.get("permissoes")
    ok = await contas_de(request.app).salvar_membro(
        int(request.match_info["id"]),
        [p for p in perms if p in PERMISSOES] if isinstance(perms, list) else None,
        bool(corpo["ativo"]) if "ativo" in corpo else None)
    return _ok() if ok else _erro("Membro não encontrado.", 404)


@web.middleware
async def protecao_csrf(request: web.Request, handler):
    """POST com cookie de sessão (sem initData) precisa do cabeçalho que só o nosso JS manda."""
    if (request.method == "POST" and request.path.startswith("/api/") and COOKIE in request.cookies
            and not request.headers.get("X-Telegram-Init-Data") and request.headers.get("X-Requested-With") != "painel"):
        return _erro("requisicao_invalida", 403)
    return await handler(request)


def registrar_rotas(app: web.Application) -> None:
    app.middlewares.insert(0, protecao_csrf)
    for caminho in ("/painel", "/painel/cadastro", "/painel/redefinir"):
        app.router.add_get(caminho, pagina)
    app.router.add_get("/api/conta/convite/{codigo}", info_convite)
    app.router.add_post("/api/conta/cadastrar", cadastrar)
    app.router.add_post("/api/conta/entrar", entrar)
    app.router.add_post("/api/conta/sair", sair)
    app.router.add_get("/api/conta/eu", eu)
    app.router.add_post("/api/conta/esqueci", esqueci)
    app.router.add_post("/api/conta/redefinir", redefinir)
    app.router.add_post("/api/conta/senha", mudar_senha)
    app.router.add_post("/api/conta/sair_todos", sair_todos)
    app.router.add_get("/api/conta/minha", minha)
    app.router.add_get("/api/criadora/acesso", acesso)
    app.router.add_post("/api/criadora/equipe/convidar", convidar)
    app.router.add_post("/api/criadora/equipe/convite/{codigo}/cancelar", cancelar_convite)
    app.router.add_post("/api/criadora/equipe/{id:\\d+}", membro)


# ════════════════════════════════════════════════════════════════════════════
#  Bot: /convite (dono) e /start vc_... (vínculo)
# ════════════════════════════════════════════════════════════════════════════
CMDS_ADMIN: list[BotCommand] = []        # main.py preenche (para dar os comandos à criadora ao vincular)


def get_router(cfg: Config, contas: Contas) -> Router:
    router = Router(name="contas")
    router.message.filter(F.chat.type == "private")

    @router.message(Command("convite"), F.from_user.id.in_(cfg.dono_ids))
    async def convite(msg: Message, command: CommandObject):
        nome = (command.args or "").strip()
        codigo = await contas.criar_convite("dona", TODAS, msg.from_user.id, nome)
        link = f"{cfg.public_base_url.rstrip('/')}/painel/cadastro?c={codigo}"
        await msg.answer(f"✉️ <b>Convite de criadora</b>{' para ' + html.escape(nome) if nome else ''}\n"
                         f"Mande este link para ela (vale 7 dias, uso único):\n{html.escape(link)}\n\n"
                         "Ela cria a conta com e-mail e senha, vincula o Telegram e vira admin deste bot.",
                         disable_web_page_preview=True)

    @router.message(CommandStart(deep_link=True, magic=F.args.startswith("vc_")))
    async def vincular(msg: Message, command: CommandObject, bot: Bot):
        conta_id = ler_vinculo(cfg, command.args)
        if not conta_id:
            await msg.answer("Esse link de vínculo venceu. Abra o painel e toque em vincular de novo.")
            return
        r = await contas.vincular(conta_id, msg.from_user.id)
        if r == "outra_conta":
            await msg.answer("Este Telegram já está vinculado a outra conta do painel.")
            return
        if r != "ok":
            await msg.answer("Não encontrei essa conta.")
            return
        conta = contas.cache[conta_id]
        if conta["papel"] == "dona":
            try:
                await bot.set_my_commands(CMDS_ADMIN, scope=BotCommandScopeChat(chat_id=msg.from_user.id))
            except Exception:
                pass
        log.info("Conta %s vinculada ao Telegram %s", conta_id, msg.from_user.id)
        from loja import url_loja
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
        url = url_loja(cfg)
        teclado = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="👑 Abrir painel", web_app=WebAppInfo(url=url))]]) if url else None
        await msg.answer(f"✅ <b>Pronto, {html.escape(conta['nome'])}!</b> Seu Telegram está vinculado.\n"
                         + ("Agora você é a criadora deste bot: a loja abre no modo criadora e o /admin funciona aqui."
                            if conta["papel"] == "dona" else
                            "Você entrou para a equipe. Abra a loja para ver o que pode fazer."),
                         reply_markup=teclado)

    return router
