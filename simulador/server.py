"""
SIMULADOR LOCAL DO TELEGRAM
===========================
Um "falso" servidor da Bot API do Telegram + uma interface web parecida com o Telegram.

O bot (main.py) roda SEM nenhuma alteração: ele apenas aponta para
http://localhost:8081 em vez de https://api.telegram.org (variável TELEGRAM_API_BASE).
Assim os mesmos handlers, FSM, blur, PIX e webhook são testados de ponta a ponta,
sem token real e sem logar em nenhuma conta.

Personagens simulados:
  👑 Criadora (admin)  id 111
  👤 Cliente           id 222
  📢 Canal de Amostras id -1001234567890
"""
import asyncio
import itertools
import json
import logging
import os
import secrets
import time
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from aiohttp import web
from PIL import Image

log = logging.getLogger("simulador")

AQUI = Path(__file__).parent
BOT_WEBHOOK_URL = os.getenv("BOT_WEBHOOK_URL", "http://127.0.0.1:8080")

BOT_USER = {"id": 7000000001, "is_bot": True, "first_name": "Packs VIP", "username": "PacksVipBot",
            "can_join_groups": False, "can_read_all_group_messages": False, "supports_inline_queries": False}
ADMIN = {"id": 111, "is_bot": False, "first_name": "Criadora", "username": "criadora", "language_code": "pt-br"}
CLIENTE = {"id": 222, "is_bot": False, "first_name": "Cliente", "username": "cliente_teste", "language_code": "pt-br"}
CANAL = {"id": -1001234567890, "type": "channel", "title": "Canal de Amostras 🔥", "username": "amostras_vip"}
VIP = {"id": -1009876543210, "type": "channel", "title": "VIP 🔒 (privado)"}
CANAIS = {CANAL["id"]: CANAL, VIP["id"]: VIP}
AJUDANTE = {"id": 444, "is_bot": False, "first_name": "Ana", "username": "ana_equipe", "language_code": "pt-br"}
USUARIOS = {"admin": ADMIN, "client": CLIENTE, "ajudante": AJUDANTE}
# Usuário fictício que "bloqueou o bot": qualquer envio para ele devolve 403 (testa o broadcast)
BLOQUEADO_ID = 333
LIMITE_POR_SEGUNDO = 30          # acima disso a falsa API responde 429, como o Telegram

# Campos da Bot API que chegam como JSON dentro do form-data
CAMPOS_JSON = {"chat_id", "from_chat_id", "message_id", "reply_markup", "media", "protect_content", "entities",
               "caption_entities", "show_alert", "offset", "limit", "timeout", "allowed_updates",
               "drop_pending_updates", "disable_notification", "scope", "commands", "link_preview_options",
               "reply_parameters", "message_ids", "user_id", "creates_join_request", "member_limit",
               "only_if_banned", "reaction", "expire_date", "menu_button"}


class EstadoSim:
    def __init__(self):
        self.ids = itertools.count(1)
        self.update_ids = itertools.count(1)
        self.chats: dict[int, list[dict]] = {ADMIN["id"]: [], CLIENTE["id"]: [], AJUDANTE["id"]: [], CANAL["id"]: [], VIP["id"]: []}
        self.arquivos: dict[str, dict] = {}            # file_id -> {"bytes", "tipo", "w", "h"}
        self.updates: list[dict] = []
        self.cond = asyncio.Condition()
        self.callbacks: dict[str, int] = {}              # callback_query_id -> chat_id
        self.toasts: list[dict] = []
        self.versao = 0
        self.ultimo_poll = 0.0
        self.envios: list[float] = []                    # horários dos últimos envios (rate limit)
        self.membros_vip: set[int] = set()
        self.convites: dict[str, dict] = {}              # invite_link -> ChatInviteLink
        self.eventos: list[dict] = []                    # log do canal VIP (entrou/saiu/recusado)
        self.token = ""                                  # token usado pelo bot (para assinar o initData)
        self.menu_loja: str | None = None                # URL do botão de menu (Mini App)

    def mudou(self):
        self.versao += 1

    # ── arquivos ───────────────────────────────────────────────────────────
    def salvar_arquivo(self, dados: bytes, nome: str = "", tipo: str | None = None) -> dict:
        file_id = "SIM" + secrets.token_urlsafe(18)
        info = {"bytes": dados, "tipo": tipo or "photo", "w": 0, "h": 0, "nome": nome}
        if tipo != "document":
            try:
                with Image.open(BytesIO(dados)) as img:
                    info["w"], info["h"] = img.size
            except Exception:
                info["tipo"] = "video"
                info["w"], info["h"] = 640, 360
        self.arquivos[file_id] = info
        return {"file_id": file_id, "file_unique_id": file_id[-12:], "width": info["w"],
                "height": info["h"], "file_size": len(dados)}

    def midia_campo(self, file_id: str) -> dict:
        info = self.arquivos[file_id]
        base = {"file_id": file_id, "file_unique_id": file_id[-12:], "width": info["w"],
                "height": info["h"], "file_size": len(info["bytes"])}
        if info["tipo"] == "photo":
            return {"photo": [base]}
        if info["tipo"] == "document":
            return {"document": {"file_id": file_id, "file_unique_id": file_id[-12:],
                                 "file_name": info["nome"] or "arquivo", "file_size": len(info["bytes"])}}
        return {"video": {**base, "duration": 1, "mime_type": "video/mp4"}}

    # ── mensagens ──────────────────────────────────────────────────────────
    def chat_obj(self, chat_id: int) -> dict:
        if chat_id in CANAIS:
            return CANAIS[chat_id]
        u = ADMIN if chat_id == ADMIN["id"] else CLIENTE
        return {"id": u["id"], "type": "private", "first_name": u["first_name"], "username": u["username"]}

    def nova_msg(self, chat_id: int, de: dict | None, **campos) -> dict:
        msg = {"message_id": next(self.ids), "date": int(time.time()), "chat": self.chat_obj(chat_id)}
        if de:
            msg["from"] = de
        elif chat_id in CANAIS:
            msg["sender_chat"] = CANAIS[chat_id]
        msg.update({k: v for k, v in campos.items() if v is not None})
        self.chats.setdefault(chat_id, []).append(msg)
        self.mudou()
        return msg

    def achar_msg(self, chat_id: int, message_id: int) -> dict | None:
        return next((m for m in self.chats.get(chat_id, []) if m["message_id"] == message_id), None)

    async def push_update(self, **conteudo):
        async with self.cond:
            self.updates.append({"update_id": next(self.update_ids), **conteudo})
            self.cond.notify_all()


S = EstadoSim()


def remetente(chat_id: int) -> dict | None:
    return None if chat_id in CANAIS else BOT_USER


def evento_vip(texto: str):
    S.eventos.append({"t": time.time(), "texto": texto})
    S.eventos = S.eventos[-30:]
    S.mudou()


def ok(result) -> web.Response:
    return web.json_response({"ok": True, "result": result})


def erro(desc: str, code: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error_code": code, "description": f"Bad Request: {desc}"}, status=code)


def publico(msg: dict) -> dict:
    """Remove campos internos (prefixo _) antes de devolver ao bot."""
    return {k: v for k, v in msg.items() if not k.startswith("_")}


# ════════════════════════════════════════════════════════════════════════════
#  FALSA BOT API  ->  /bot{token}/{metodo}
# ════════════════════════════════════════════════════════════════════════════
async def ler_params(request: web.Request) -> tuple[dict, dict]:
    params, uploads = {}, {}
    if request.content_type == "application/json":
        return await request.json(), {}
    form = await request.post()
    for k, v in form.items():
        if hasattr(v, "file"):
            uploads[k] = (v.file.read(), v.filename)
        elif k in CAMPOS_JSON:
            try:
                params[k] = json.loads(v)
            except (ValueError, TypeError):
                params[k] = v
        else:
            params[k] = v
    return params, uploads


def resolver_midia(valor: str, uploads: dict, campo: str) -> str:
    """Converte 'attach://x' / upload direto / file_id existente em um file_id do simulador."""
    tipo = "document" if campo == "document" else None
    if isinstance(valor, str) and valor.startswith("attach://"):
        dados, nome = uploads[valor[len("attach://"):]]
        return S.salvar_arquivo(dados, nome, tipo)["file_id"]
    if campo in uploads:
        dados, nome = uploads[campo]
        return S.salvar_arquivo(dados, nome, tipo)["file_id"]
    if valor in S.arquivos:
        return valor
    raise KeyError(valor)


def chat_valido(params: dict) -> int:
    chat_id = int(params["chat_id"])
    if chat_id == BLOQUEADO_ID:
        raise web.HTTPForbidden(text=json.dumps({"ok": False, "error_code": 403,
                                                 "description": "Forbidden: bot was blocked by the user"}),
                                content_type="application/json")
    if chat_id not in S.chats:
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error_code": 400,
                                                  "description": "Bad Request: chat not found"}),
                                 content_type="application/json")
    return chat_id


async def api(request: web.Request) -> web.Response:
    metodo = request.match_info["method"].lower()
    S.token = request.match_info["token"]
    params, uploads = await ler_params(request)
    try:
        return await despachar(metodo, params, uploads)
    except KeyError as e:
        return erro(f"wrong file identifier/HTTP URL specified ({e})")


async def despachar(metodo: str, p: dict, uploads: dict) -> web.Response:
    extras = lambda: {"reply_markup": p.get("reply_markup"),
                      "has_protected_content": True if p.get("protect_content") else None}

    if metodo == "getme":
        return ok(BOT_USER)

    if metodo.startswith(("send", "copy")) and metodo != "sendchataction":
        agora = time.time()
        S.envios = [t for t in S.envios if agora - t < 1] + [agora]
        if len(S.envios) > LIMITE_POR_SEGUNDO:
            return web.json_response({"ok": False, "error_code": 429,
                                      "description": "Too Many Requests: retry after 1",
                                      "parameters": {"retry_after": 1}}, status=429)

    if metodo == "getupdates":
        S.ultimo_poll = time.time()
        offset = int(p.get("offset") or 0)
        timeout = float(p.get("timeout") or 0)
        async with S.cond:
            S.updates = [u for u in S.updates if u["update_id"] >= offset]
            if not S.updates and timeout:
                try:
                    await asyncio.wait_for(S.cond.wait_for(lambda: bool(S.updates)), timeout)
                except asyncio.TimeoutError:
                    pass
            return ok(S.updates[: int(p.get("limit") or 100)])

    if metodo == "sendmessage":
        chat_id = chat_valido(p)
        return ok(publico(S.nova_msg(chat_id, remetente(chat_id),
                                     text=p["text"], **extras())))

    if metodo in ("sendphoto", "sendvideo", "senddocument", "sendanimation"):
        campo = metodo[4:]
        chat_id = chat_valido(p)
        file_id = resolver_midia(p.get(campo), uploads, campo)
        if p.get("width") and p.get("height") and file_id in S.arquivos:   # como o Telegram: usa as medidas informadas
            S.arquivos[file_id]["w"], S.arquivos[file_id]["h"] = int(p["width"]), int(p["height"])
        msg = S.nova_msg(chat_id, remetente(chat_id),
                         caption=p.get("caption"), **S.midia_campo(file_id), **extras())
        return ok(publico(msg))

    if metodo == "sendmediagroup":
        chat_id = chat_valido(p)
        grupo = secrets.token_hex(6)
        enviados = []
        for item in p["media"]:
            file_id = resolver_midia(item["media"], uploads, "")
            enviados.append(publico(S.nova_msg(
                chat_id, remetente(chat_id), media_group_id=grupo,
                caption=item.get("caption"), **S.midia_campo(file_id),
                has_protected_content=True if p.get("protect_content") else None)))
        return ok(enviados)

    if metodo == "copymessage":
        chat_id = chat_valido(p)
        orig = S.achar_msg(int(p["from_chat_id"]), int(p["message_id"]))
        if not orig:
            return erro("message to copy not found")
        campos = {k: orig[k] for k in ("text", "entities", "caption", "photo", "video", "document") if k in orig}
        if "caption" in p:
            campos["caption"] = p["caption"]
        msg = S.nova_msg(chat_id, remetente(chat_id), **campos, **extras())
        return ok({"message_id": msg["message_id"]})

    if metodo in ("editmessagetext", "editmessagecaption", "editmessagereplymarkup"):
        msg = S.achar_msg(int(p["chat_id"]), int(p["message_id"]))
        if not msg:
            return erro("message to edit not found")
        if metodo == "editmessagetext":
            if msg.get("text") == p["text"] and msg.get("reply_markup") == p.get("reply_markup"):
                return erro("message is not modified")
            msg["text"] = p["text"]
        elif metodo == "editmessagecaption":
            msg["caption"] = p.get("caption")
        msg.pop("reply_markup", None)
        if p.get("reply_markup"):
            msg["reply_markup"] = p["reply_markup"]
        msg["edit_date"] = int(time.time())
        S.mudou()
        return ok(publico(msg))

    if metodo == "deletemessage":
        lista = S.chats.get(int(p["chat_id"]), [])
        S.chats[int(p["chat_id"])] = [m for m in lista if m["message_id"] != int(p["message_id"])]
        S.mudou()
        return ok(True)

    if metodo == "getfile":
        file_id = p["file_id"]
        if file_id not in S.arquivos:
            return erro("invalid file_id")
        info = S.arquivos[file_id]
        ext = "jpg" if info["tipo"] == "photo" else "mp4"
        return ok({"file_id": file_id, "file_unique_id": file_id[-12:], "file_size": len(info["bytes"]),
                   "file_path": f"{info['tipo']}s/{file_id}.{ext}"})

    if metodo == "answercallbackquery":
        if p.get("text"):
            S.toasts.append({"chat_id": S.callbacks.get(p["callback_query_id"]), "text": p["text"],
                             "alert": bool(p.get("show_alert")), "t": time.time()})
            S.mudou()
        return ok(True)

    # ── canal VIP: convites, pedidos de entrada, banimentos ────────────────
    if metodo == "createchatinvitelink":
        link = "https://t.me/+SIM" + secrets.token_urlsafe(10)
        convite = {"invite_link": link, "creator": BOT_USER, "creates_join_request": bool(p.get("creates_join_request")),
                   "is_primary": False, "is_revoked": False, "name": p.get("name"),
                   "member_limit": p.get("member_limit"), "_chat_id": int(p["chat_id"])}
        S.convites[link] = convite
        return ok(publico(convite))

    if metodo in ("approvechatjoinrequest", "declinechatjoinrequest"):
        user_id = int(p["user_id"])
        nome = "Cliente" if user_id == CLIENTE["id"] else str(user_id)
        if metodo == "approvechatjoinrequest":
            S.membros_vip.add(user_id)
            evento_vip(f"✅ {nome} entrou no VIP (pedido aprovado pelo bot)")
        else:
            evento_vip(f"⛔ Pedido de {nome} recusado pelo bot")
        return ok(True)

    if metodo == "banchatmember":
        user_id = int(p["user_id"])
        if user_id in S.membros_vip:
            S.membros_vip.discard(user_id)
            evento_vip(f"🚪 {'Cliente' if user_id == CLIENTE['id'] else user_id} foi removido do VIP")
        return ok(True)

    if metodo in ("unbanchatmember", "setmessagereaction"):
        if metodo == "setmessagereaction":
            msg = S.achar_msg(int(p["chat_id"]), int(p["message_id"]))
            if msg:
                msg["_reacao"] = "".join(r.get("emoji", "") for r in (p.get("reaction") or []))
                S.mudou()
        return ok(True)

    if metodo == "getchatmember":
        user_id = int(p["user_id"])
        user = CLIENTE if user_id == CLIENTE["id"] else ADMIN if user_id == ADMIN["id"] else \
            {"id": user_id, "is_bot": False, "first_name": str(user_id)}
        if int(p["chat_id"]) == VIP["id"] and user_id in S.membros_vip:
            return ok({"status": "member", "user": user})
        return ok({"status": "left", "user": user})

    if metodo == "setchatmenubutton":
        botao = p.get("menu_button") or {}
        if isinstance(botao, str):
            botao = json.loads(botao)
        S.menu_loja = (botao.get("web_app") or {}).get("url")
        S.mudou()
        return ok(True)

    if metodo == "getchat":
        return ok(S.chat_obj(int(p["chat_id"])))

    # deleteWebhook, setMyCommands, sendChatAction, etc.
    return ok(True)


async def baixar_arquivo(request: web.Request) -> web.Response:
    file_id = Path(request.match_info["path"]).stem
    info = S.arquivos.get(file_id)
    if not info:
        raise web.HTTPNotFound()
    return web.Response(body=info["bytes"])


# ════════════════════════════════════════════════════════════════════════════
#  API DA INTERFACE WEB  ->  /sim/...
# ════════════════════════════════════════════════════════════════════════════
def _entidades(texto: str) -> list[dict] | None:
    if texto.startswith("/"):
        return [{"type": "bot_command", "offset": 0, "length": len(texto.split()[0])}]
    return None


async def sim_enviar_texto(request: web.Request) -> web.Response:
    d = await request.json()
    user = USUARIOS[d["who"]]
    texto = d["text"].strip()
    if not texto:
        return web.json_response({"ok": False})
    resposta = S.achar_msg(user["id"], int(d["reply_to"])) if d.get("reply_to") else None
    msg = S.nova_msg(user["id"], user, text=texto, entities=_entidades(texto),
                     reply_to_message=publico(resposta) if resposta else None)
    await S.push_update(message=msg)
    return web.json_response({"ok": True})


async def sim_upload(request: web.Request) -> web.Response:
    form = await request.post()
    user = USUARIOS[form["who"]]
    arquivos = [f for k, f in form.items() if k == "files"]
    grupo = secrets.token_hex(6) if len(arquivos) > 1 else None
    for f in arquivos:
        info = S.salvar_arquivo(f.file.read(), f.filename)
        msg = S.nova_msg(user["id"], user, media_group_id=grupo, **S.midia_campo(info["file_id"]))
        await S.push_update(message=msg)
    return web.json_response({"ok": True})


async def sim_callback(request: web.Request) -> web.Response:
    d = await request.json()
    user = USUARIOS[d["who"]]
    msg = S.achar_msg(int(d["chat_id"]), int(d["message_id"]))
    if not msg:
        return web.json_response({"ok": False})
    cb_id = secrets.token_hex(8)
    S.callbacks[cb_id] = user["id"]
    await S.push_update(callback_query={"id": cb_id, "from": user, "chat_instance": str(msg["chat"]["id"]),
                                        "message": publico(msg), "data": d["data"]})
    return web.json_response({"ok": True})


async def sim_deeplink(request: web.Request) -> web.Response:
    """Simula o clique em https://t.me/Bot?start=xxx -> envia '/start xxx' no privado."""
    d = await request.json()
    user = USUARIOS[d["who"]]
    url = urlparse(d["url"])
    start = parse_qs(url.query).get("start", [""])[0]
    texto = f"/start {start}".strip()
    msg = S.nova_msg(user["id"], user, text=texto, entities=_entidades(texto))
    await S.push_update(message=msg)
    return web.json_response({"ok": True})


async def sim_entrar_link(request: web.Request) -> web.Response:
    """Simula o clique num link de convite t.me/+xxxx."""
    d = await request.json()
    user = USUARIOS[d["who"]]
    convite = S.convites.get(d["url"])
    if not convite:
        return web.json_response({"ok": False, "erro": "Link de convite inválido"})
    if user["id"] in S.membros_vip:
        return web.json_response({"ok": True, "msg": "Você já está no canal VIP"})
    if convite["creates_join_request"]:
        evento_vip(f"🙋 {user['first_name']} pediu para entrar")
        await S.push_update(chat_join_request={"chat": VIP, "from": user, "user_chat_id": user["id"],
                                               "date": int(time.time()), "invite_link": publico(convite)})
        return web.json_response({"ok": True, "msg": "Pedido de entrada enviado"})
    S.membros_vip.add(user["id"])
    evento_vip(f"✅ {user['first_name']} entrou no VIP pelo link")
    return web.json_response({"ok": True, "msg": "Entrou no canal"})


async def sim_postar_canal(request: web.Request) -> web.Response:
    """A criadora posta direto no canal (como faria no app do Telegram)."""
    form = await request.post()
    canal_id = VIP["id"] if form.get("canal") == "vip" else CANAL["id"]
    arquivos = [f for k, f in form.items() if k == "files"]
    texto = (form.get("text") or "").strip()
    grupo = secrets.token_hex(6) if len(arquivos) > 1 else None
    for i, f in enumerate(arquivos):
        info = S.salvar_arquivo(f.file.read(), f.filename)
        S.nova_msg(canal_id, None, media_group_id=grupo, caption=texto if i == 0 and texto else None,
                   **S.midia_campo(info["file_id"]))
    if texto and not arquivos:
        S.nova_msg(canal_id, None, text=texto)
    return web.json_response({"ok": True})


def gerar_init_data(user: dict) -> str:
    """initData igual ao do Telegram: campos + hash HMAC-SHA256 (chave derivada do token do bot)."""
    import hashlib
    import hmac
    from urllib.parse import urlencode
    dados = {
        "query_id": "AAH" + secrets.token_urlsafe(12),
        "user": json.dumps({"id": user["id"], "first_name": user["first_name"], "username": user["username"],
                            "language_code": "pt-br", "allows_write_to_pm": True}, separators=(",", ":"),
                           ensure_ascii=False),
        "auth_date": str(int(time.time())),
    }
    verificacao = "\n".join(f"{k}={v}" for k, v in sorted(dados.items()))
    segredo = hmac.new(b"WebAppData", S.token.encode(), hashlib.sha256).digest()
    dados["hash"] = hmac.new(segredo, verificacao.encode(), hashlib.sha256).hexdigest()
    return urlencode(dados)


async def sim_init_data(request: web.Request) -> web.Response:
    return web.json_response({"init_data": gerar_init_data(USUARIOS[request.query.get("who", "client")])})


async def sim_estado(request: web.Request) -> web.Response:
    if int(request.query.get("v", -1)) == S.versao:
        return web.json_response({"v": S.versao, "same": True, "online": time.time() - S.ultimo_poll < 30})
    agora = time.time()
    S.toasts = [t for t in S.toasts if agora - t["t"] < 8]
    return web.json_response({
        "v": S.versao,
        "online": agora - S.ultimo_poll < 30,
        "bot": BOT_USER, "admin": ADMIN, "client": CLIENTE, "canal": CANAL, "vip": VIP,
        "chats": {"admin": S.chats[ADMIN["id"]], "client": S.chats[CLIENTE["id"]], "ajudante": S.chats[AJUDANTE["id"]], "canal": S.chats[CANAL["id"]],
                  "vip": S.chats[VIP["id"]]},
        "cliente_no_vip": CLIENTE["id"] in S.membros_vip,
        "menu_loja": S.menu_loja,
        "eventos_vip": S.eventos,
        "arquivos": {fid: info["tipo"] for fid, info in S.arquivos.items()},
        "toasts": S.toasts,
    })


async def sim_arquivo(request: web.Request) -> web.Response:
    info = S.arquivos.get(request.match_info["file_id"])
    if not info:
        raise web.HTTPNotFound()
    if info["tipo"] == "document":
        return web.Response(body=info["bytes"], content_type="application/octet-stream",
                            headers={"Content-Disposition": f'attachment; filename="{info["nome"] or "arquivo"}"'})
    tipo = "image/jpeg" if info["tipo"] == "photo" else "video/mp4"
    return web.Response(body=info["bytes"], content_type=tipo)


async def sim_cobrancas(_: web.Request) -> web.Response:
    """Proxy para o mock de gateway do bot (evita CORS no navegador)."""
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{BOT_WEBHOOK_URL}/mock/cobrancas")
        return web.json_response(r.json() if r.status_code == 200 else [])
    except httpx.HTTPError:
        return web.json_response([])


async def sim_pagar(request: web.Request) -> web.Response:
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(f"{BOT_WEBHOOK_URL}/mock/pagar/{request.match_info['pix_id']}")
    return web.json_response(r.json(), status=r.status_code)


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(AQUI / "static" / "index.html")


def criar_app() -> web.Application:
    app = web.Application(client_max_size=200 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_post("/bot{token}/{method}", api)
    app.router.add_get("/bot{token}/{method}", api)
    app.router.add_get("/file/bot{token}/{path:.*}", baixar_arquivo)
    app.router.add_post("/sim/texto", sim_enviar_texto)
    app.router.add_post("/sim/upload", sim_upload)
    app.router.add_post("/sim/callback", sim_callback)
    app.router.add_post("/sim/deeplink", sim_deeplink)
    app.router.add_post("/sim/entrar", sim_entrar_link)
    app.router.add_get("/sim/initdata", sim_init_data)
    app.router.add_post("/sim/canal", sim_postar_canal)
    app.router.add_get("/sim/estado", sim_estado)
    app.router.add_get("/sim/arquivo/{file_id}", sim_arquivo)
    app.router.add_get("/sim/cobrancas", sim_cobrancas)
    app.router.add_post("/sim/pagar/{pix_id}", sim_pagar)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(criar_app(), host="127.0.0.1", port=int(os.getenv("SIM_PORT", "8081")))
