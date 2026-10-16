"""
Conta de recebimento da criadora (Mercado Pago ou Pushin Pay) e split do dono da plataforma.

  - Mercado Pago (marketplace): o dono cria uma aplicação na conta Mercado Pago dele e põe MP_CLIENT_ID e
    MP_CLIENT_SECRET no ambiente. A criadora toca em "Conectar Mercado Pago", autoriza no site do Mercado Pago
    (OAuth) e volta. Cada PIX é criado com o token dela e a `application_fee` (a % do /split) cai sozinha na
    conta do dono. O token dela vale 180 dias e é renovado automaticamente (loop_renovar_mp).
  - Pushin Pay: a criadora cola o token da conta Pushin Pay dela em Ajustes › Vendas › Recebimento. O token é validado
    na Pushin Pay, salvo na tabela ajustes (pushinpay_token) e nunca volta para o app (só os 4 últimos dígitos).
  - O dono da plataforma (DONO_IDS) cadastra a conta dele para receber o split com /split <account_id> <pct>.
    Fica salvo em ajustes (split_conta / split_pct) e vale no lugar de PLATAFORMA_ACCOUNT_ID / PLATAFORMA_PCT.
"""
import asyncio
import hashlib
import hmac
import html
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from aiohttp import web

from config import Config
from database import Database
from payments import MercadoPagoGateway, PushinPayGateway

log = logging.getLogger(__name__)
TOKEN_OK = re.compile(r"^[A-Za-z0-9|._\-]{20,400}$")
CONTA_OK = re.compile(r"^[A-Za-z0-9\-]{8,80}$")


def _dona(request: web.Request) -> dict:
    """Só a criadora dona da loja (não a equipe): é a conta para onde vai o dinheiro."""
    from loja import _usuario
    user = _usuario(request)
    if user["id"] not in request.app["cfg"].admin_ids:
        raise web.HTTPForbidden(text='{"ok":false,"erro":"Só a criadora pode mexer na conta de recebimento."}',
                                content_type="application/json")
    return user


async def _estado(app: web.Application) -> dict:
    db: Database = app["db"]
    cfg: Config = app["cfg"]
    gw = app["gateway"]
    salvo = await db.get_ajuste("pushinpay_token") or ""
    conta, pct = await gw.split()
    ativo = await gw.ativo()
    mp = bool(await gw.token_mp())
    return {"ok": True, "ativo": ativo,
            "gateway": {"mercadopago": "Mercado Pago", "pushinpay": "Pushin Pay"}.get(ativo, ""),
            "conectado": bool(ativo), "final": salvo[-4:] if salvo else "",
            "pelo_servidor": not salvo and bool(cfg.pushinpay_token), "pushin": bool(salvo),
            "mp": {"disponivel": bool(cfg.mp_client_id and cfg.mp_client_secret), "conectado": mp,
                   "conta": await db.get_ajuste("mp_apelido") or "" if mp else ""},
            "em_teste": await gw.em_teste(),
            "taxa_pct": pct if (ativo == "mercadopago" or (ativo == "pushinpay" and conta)
                                or (not ativo and (conta or cfg.mp_client_id))) else 0}


async def ver(request: web.Request) -> web.Response:
    _dona(request)
    return web.json_response(await _estado(request.app))


async def conectar(request: web.Request) -> web.Response:
    user = _dona(request)
    try:
        corpo = await request.json()
    except Exception:
        corpo = {}
    token = re.sub(r"\s+", "", str(corpo.get("token", "")))
    if token.lower().startswith("bearer"):
        token = token[6:]
    if not TOKEN_OK.match(token):
        return web.json_response({"ok": False, "erro": "Esse token não parece certo. Copie de novo na Pushin Pay."},
                                 status=400)
    gw = PushinPayGateway(request.app["cfg"], token)
    try:
        valido = await gw.token_valido()
    finally:
        await gw.http.aclose()
    if valido is False:
        return web.json_response({"ok": False, "erro": "A Pushin Pay recusou esse token. Confira se copiou inteiro."},
                                 status=400)
    await request.app["db"].set_ajuste("pushinpay_token", token)
    await request.app["db"].conn.execute(_SQL_APAGAR_MP)        # uma conta de recebimento por vez
    await request.app["db"].conn.commit()
    log.info("Conta de recebimento conectada por %s (token ...%s)", user["id"], token[-4:])
    estado = await _estado(request.app)
    if valido is None:
        estado["aviso"] = "Salvei, mas a Pushin Pay não respondeu agora para confirmar. Faça uma compra de teste."
    return web.json_response(estado)


async def desconectar(request: web.Request) -> web.Response:
    user = _dona(request)
    db: Database = request.app["db"]
    await db.conn.execute("DELETE FROM ajustes WHERE chave = 'pushinpay_token'")
    await db.conn.commit()
    log.info("Conta de recebimento desconectada por %s", user["id"])
    return web.json_response(await _estado(request.app))


# ── Mercado Pago (OAuth) ────────────────────────────────────────────────────
MP_AUTH = "https://auth.mercadopago.com/authorization"
_SQL_APAGAR_MP = ("DELETE FROM ajustes WHERE chave IN "
                  "('mp_access_token', 'mp_refresh_token', 'mp_user_id', 'mp_expira', 'mp_apelido')")
MP_TOKEN = f"{MercadoPagoGateway.BASE}/oauth/token"


def _retorno(cfg: Config) -> str:
    """Tem que ser cadastrada igualzinha em "URLs de redirecionamento" na aplicação do Mercado Pago."""
    return f"{cfg.public_base_url.rstrip('/')}/mp/retorno"


def _assinar(cfg: Config, texto: str) -> str:
    return hmac.new(f"mp-oauth:{cfg.webhook_secret}".encode(), texto.encode(), hashlib.sha256).hexdigest()[:32]


def _estado_oauth(cfg: Config, user_id: int) -> str:
    base = f"{user_id}.{int(time.time())}"
    return f"{base}.{_assinar(cfg, base)}"


def _ler_estado_oauth(cfg: Config, estado: str) -> int | None:
    try:
        uid, ts, sig = estado.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _assinar(cfg, f"{uid}.{ts}")) or time.time() - int(ts) > 30 * 60:
        return None
    return int(uid)


async def _salvar_tokens_mp(db: Database, dados: dict) -> None:
    expira = datetime.now(timezone.utc) + timedelta(seconds=int(dados.get("expires_in") or 15552000))
    await db.set_ajuste("mp_access_token", dados["access_token"])
    if dados.get("refresh_token"):
        await db.set_ajuste("mp_refresh_token", dados["refresh_token"])
    await db.set_ajuste("mp_user_id", str(dados.get("user_id", "")))
    await db.set_ajuste("mp_expira", expira.isoformat())


async def link_mp(request: web.Request) -> web.Response:
    user = _dona(request)
    cfg: Config = request.app["cfg"]
    if not (cfg.mp_client_id and cfg.mp_client_secret):
        return web.json_response({"ok": False, "erro": "O Mercado Pago ainda não foi liberado pelo dono da plataforma."},
                                 status=400)
    url = MP_AUTH + "?" + urlencode({"client_id": cfg.mp_client_id, "response_type": "code", "platform_id": "mp",
                                     "state": _estado_oauth(cfg, user["id"]), "redirect_uri": _retorno(cfg)})
    return web.json_response({"ok": True, "url": url})


def _pagina(titulo: str, texto: str, ok: bool) -> web.Response:
    cor = "#22c55e" if ok else "#f59e0b"
    corpo = f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(titulo)}</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0b10;color:#f4f4f5;
font:16px/1.5 system-ui,-apple-system,sans-serif;text-align:center;padding:24px;box-sizing:border-box}}
.c{{max-width:360px}}.i{{width:64px;height:64px;border-radius:50%;margin:0 auto 18px;display:grid;place-items:center;
background:{cor}22;color:{cor};font-size:32px}}h1{{font-size:22px;margin:0 0 8px}}p{{color:#a1a1aa;margin:0}}</style></head>
<body><div class="c"><div class="i">{"✓" if ok else "!"}</div><h1>{html.escape(titulo)}</h1><p>{html.escape(texto)}</p></div></body></html>"""
    return web.Response(text=corpo, content_type="text/html", headers={"Cache-Control": "no-store"})


async def retorno_mp(request: web.Request) -> web.Response:
    """O Mercado Pago volta para cá com ?code=...&state=... depois que a criadora autoriza."""
    app = request.app
    cfg: Config = app["cfg"]
    db: Database = app["db"]
    uid = _ler_estado_oauth(cfg, request.query.get("state", ""))
    code = request.query.get("code", "")
    if not uid or uid not in cfg.admin_ids:
        return _pagina("Link vencido", "Volte ao Telegram e toque em Conectar Mercado Pago de novo.", False)
    if not code:
        return _pagina("Conexão cancelada", "Nada mudou. Você pode tentar de novo pelo painel quando quiser.", False)
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(MP_TOKEN, json={"client_id": cfg.mp_client_id, "client_secret": cfg.mp_client_secret,
                                                "grant_type": "authorization_code", "code": code,
                                                "redirect_uri": _retorno(cfg)})
        if r.status_code != 200:
            log.warning("Mercado Pago recusou o código OAuth: %s %s", r.status_code, r.text[:200])
            return _pagina("Não deu certo", "O Mercado Pago recusou a conexão. Tente de novo pelo painel.", False)
        dados = r.json()
        await _salvar_tokens_mp(db, dados)
        await db.conn.execute("DELETE FROM ajustes WHERE chave = 'pushinpay_token'")
        await db.conn.commit()
        gw = MercadoPagoGateway(cfg, dados["access_token"])
        try:
            conta = await gw.conta() or {}
        finally:
            await gw.http.aclose()
        apelido = conta.get("nickname") or conta.get("first_name") or ""
        email = conta.get("email") or ""
        await db.set_ajuste("mp_apelido", apelido or (email.split("@")[0] if email else str(dados.get("user_id", ""))))
    except Exception:
        log.exception("Falha ao conectar o Mercado Pago")
        return _pagina("Não deu certo", "Não consegui falar com o Mercado Pago. Tente de novo em instantes.", False)
    log.info("Mercado Pago conectado pela criadora %s (conta %s)", uid, dados.get("user_id"))
    try:
        await app["bot"].send_message(uid, "✅ <b>Mercado Pago conectado!</b>\nA partir de agora cada PIX cai direto na sua conta.")
    except Exception:
        pass
    return _pagina("Mercado Pago conectado", "Pronto! Pode fechar esta página e voltar ao Telegram.", True)


async def desconectar_mp(request: web.Request) -> web.Response:
    user = _dona(request)
    db: Database = request.app["db"]
    await db.conn.execute(_SQL_APAGAR_MP)
    await db.conn.commit()
    log.info("Mercado Pago desconectado por %s", user["id"])
    return web.json_response(await _estado(request.app))


async def renovar_mp(cfg: Config, db: Database, forcar: bool = False) -> bool:
    """Renova o token da criadora quando faltam menos de 30 dias (ele vale 180)."""
    refresh = await db.get_ajuste("mp_refresh_token")
    expira = await db.get_ajuste("mp_expira")
    if not refresh or not cfg.mp_client_id or not cfg.mp_client_secret:
        return False
    if not forcar and expira and datetime.fromisoformat(expira) - datetime.now(timezone.utc) > timedelta(days=30):
        return False
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(MP_TOKEN, json={"client_id": cfg.mp_client_id, "client_secret": cfg.mp_client_secret,
                                            "grant_type": "refresh_token", "refresh_token": refresh})
    if r.status_code != 200:
        log.warning("Não consegui renovar o token do Mercado Pago: %s %s", r.status_code, r.text[:200])
        return False
    await _salvar_tokens_mp(db, r.json())
    log.info("Token do Mercado Pago renovado")
    return True


async def loop_renovar_mp(bot, db: Database, cfg: Config) -> None:
    avisado = False
    while True:
        try:
            await renovar_mp(cfg, db)
            expira = await db.get_ajuste("mp_expira")
            if expira and datetime.fromisoformat(expira) - datetime.now(timezone.utc) < timedelta(days=7) and not avisado:
                avisado = True
                for uid in cfg.admin_ids:
                    try:
                        await bot.send_message(uid, "⚠️ A conexão com o Mercado Pago vence em menos de 7 dias e não "
                                                    "consegui renovar. Abra Ajustes › Vendas › Recebimento e conecte de novo.")
                    except Exception:
                        pass
        except Exception:
            log.exception("Renovação do Mercado Pago falhou")
        await asyncio.sleep(12 * 3600)


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/api/criadora/recebimento", ver)
    app.router.add_post("/api/criadora/recebimento", conectar)
    app.router.add_post("/api/criadora/recebimento/desconectar", desconectar)
    app.router.add_get("/api/criadora/recebimento/mp/link", link_mp)
    app.router.add_post("/api/criadora/recebimento/mp/desconectar", desconectar_mp)
    app.router.add_get("/mp/retorno", retorno_mp)


# ════════════════════════════════════════════════════════════════════════════
#  Bot: /split (dono da plataforma)
# ════════════════════════════════════════════════════════════════════════════
def get_router(cfg: Config, db: Database, gateway) -> Router:
    router = Router(name="recebimento")
    router.message.filter(F.chat.type == "private")

    @router.message(Command("split"), F.from_user.id.in_(cfg.dono_ids))
    async def split(msg: Message, command: CommandObject):
        args = (command.args or "").split()
        if args and args[0].lower() in ("off", "desligar", "0"):
            await db.set_ajuste("split_pct", "0")
            await msg.answer("✂️ Split desligado. As vendas vão 100% para a criadora.")
            return
        if args:
            # "/split 10" (Mercado Pago: só a %) ou "/split ID_PUSHIN 10" (Pushin Pay: conta + %)
            conta = None if args[0].rstrip("%").isdigit() else args[0]
            try:
                pct = int((args[1] if conta else args[0]).replace("%", "")) if (len(args) > 1 or not conta) else 10
            except ValueError:
                pct = -1
            if (conta is not None and not CONTA_OK.match(conta)) or not 1 <= pct <= 50:
                await msg.answer("Uso:\n<code>/split 10</code> (Mercado Pago: a taxa cai na conta dona da aplicação)\n"
                                 "<code>/split ID_DA_CONTA 10</code> (Pushin Pay: o ID da sua conta Pushin Pay)\n"
                                 "A % vai de 1 a 50.")
                return
            if conta:
                await db.set_ajuste("split_conta", conta)
            await db.set_ajuste("split_pct", str(pct))
            log.info("Split configurado pelo dono %s: %s%%", msg.from_user.id, pct)
        conta, pct = await gateway.split()
        ativo = await gateway.ativo()
        mp_ok = bool(cfg.mp_client_id and cfg.mp_client_secret)
        linhas = [f"✂️ <b>Split: {pct}%</b> de cada venda" if pct else "✂️ <b>Split desligado.</b>", ""]
        linhas.append("Mercado Pago: " + ("✅ aplicação configurada, a taxa cai na sua conta Mercado Pago" if mp_ok else
                                          "⚠️ falta pôr MP_CLIENT_ID e MP_CLIENT_SECRET no Railway"))
        linhas.append("Pushin Pay: " + (f"conta <code>{html.escape(conta)}</code>" if conta else "sem conta cadastrada"))
        linhas.append("")
        linhas.append("Criadora recebendo por: " + {"mercadopago": "✅ Mercado Pago", "pushinpay": "✅ Pushin Pay"}.get(
            ativo, "⚠️ nada ainda (modo teste)"))
        if mp_ok:
            linhas.append(f"\nURL de redirecionamento da aplicação:\n<code>{html.escape(_retorno(cfg))}</code>")
        linhas.append("\n<code>/split 10</code> muda a % · <code>/split off</code> desliga")
        await msg.answer("\n".join(linhas), disable_web_page_preview=True)

    return router
