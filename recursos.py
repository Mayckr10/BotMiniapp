"""
Recursos opcionais da loja: cada um liga e desliga e tem os próprios ajustes, guardados na tabela `ajustes`.

O esquema abaixo é a fonte única: o servidor valida por ele e o Mini App monta a tela de Ajustes › Recursos
a partir dele (rótulos, ajuda, tipos e limites). Para criar um recurso novo basta acrescentar um grupo aqui
e usar `await recursos.valor(db, "chave")` onde ele age.

Tipos de campo:
  bool      chave liga/desliga
  int       número inteiro (min/max)
  pct       porcentagem inteira (min/max)
  reais     dinheiro: o app mostra "R$ 10,00", o banco guarda centavos
  texto     uma linha · textarea: várias linhas
  escolha   uma das `opcoes` [(valor, rótulo)]
  lista     números separados por vírgula (ex: meses "1,3,6")
  segredo   como texto, mas o app nunca recebe o valor de volta (só se está preenchido)
"""
from aiohttp import web

from admin_handlers import parse_preco
from database import Database, fmt_reais

# (id, título, ícone, descrição, campos, ações) — ações são botões que abrem telas próprias no app
GRUPOS = [
    # ── Vender mais ─────────────────────────────────────────────────────────
    {"id": "renovacao", "secao": "Vender mais", "titulo": "Renovação do VIP com 1 toque", "icone": "renovar",
     "descricao": "Antes de vencer, o fã recebe o aviso com o PIX do plano dele já pronto. Pagou, os dias somam.",
     "campos": [
         {"chave": "renov_pix", "tipo": "bool", "rotulo": "Mandar o PIX pronto no aviso", "padrao": False,
          "ajuda": "Desligado: o aviso vem com o botão de renovar, que gera o PIX ao tocar."},
         {"chave": "renov_dias", "tipo": "int", "rotulo": "Avisar quantos dias antes", "padrao": 3, "min": 1, "max": 10},
     ]},
    {"id": "teste", "secao": "Vender mais", "titulo": "Teste grátis do VIP", "icone": "presente",
     "descricao": "Quem nunca assinou pode experimentar o VIP de graça por alguns dias, uma vez só.",
     "campos": [
         {"chave": "teste_ativo", "tipo": "bool", "rotulo": "Oferecer teste grátis", "padrao": False},
         {"chave": "teste_dias", "tipo": "int", "rotulo": "Duração do teste (dias)", "padrao": 3, "min": 1, "max": 14,
          "depende": "teste_ativo"},
     ]},
    {"id": "volta", "secao": "Vender mais", "titulo": "Oferta de volta para ex-assinantes", "icone": "etiqueta",
     "descricao": "Quando o VIP vence e o fã não renova, o bot manda um cupom pessoal depois de alguns dias.",
     "campos": [
         {"chave": "volta_ativo", "tipo": "bool", "rotulo": "Mandar oferta de volta", "padrao": False},
         {"chave": "volta_dias", "tipo": "int", "rotulo": "Dias depois de vencer", "padrao": 3, "min": 1, "max": 60,
          "depende": "volta_ativo"},
         {"chave": "volta_pct", "tipo": "pct", "rotulo": "Desconto do cupom", "padrao": 30, "min": 5, "max": 90,
          "depende": "volta_ativo"},
         {"chave": "volta_horas", "tipo": "int", "rotulo": "Validade do cupom (horas)", "padrao": 48, "min": 6, "max": 336,
          "depende": "volta_ativo"},
         {"chave": "volta_msg", "tipo": "textarea", "rotulo": "Mensagem", "depende": "volta_ativo", "max": 500,
          "padrao": "Sinto sua falta no VIP, {nome} 🥺 Volta com {desconto} de desconto? O cupom vale até {validade}."},
     ]},
    {"id": "upsell", "secao": "Vender mais", "titulo": "Leve também (upsell)", "icone": "subir",
     "descricao": "Logo depois de uma compra, o fã vê outro pack com desconto por tempo limitado.",
     "campos": [
         {"chave": "upsell_ativo", "tipo": "bool", "rotulo": "Oferecer outro pack depois da compra", "padrao": False},
         {"chave": "upsell_pct", "tipo": "pct", "rotulo": "Desconto da oferta", "padrao": 20, "min": 5, "max": 70,
          "depende": "upsell_ativo"},
         {"chave": "upsell_minutos", "tipo": "int", "rotulo": "A oferta vale por (minutos)", "padrao": 60, "min": 10,
          "max": 1440, "depende": "upsell_ativo"},
     ]},
    {"id": "relampago", "secao": "Vender mais", "titulo": "Promoção relâmpago", "icone": "raio",
     "descricao": "Desconto num pack por poucas horas, com contagem regressiva no card. Você liga em cada pack (menu ⋯).",
     "campos": [
         {"chave": "relampago_ativo", "tipo": "bool", "rotulo": "Permitir promoções relâmpago", "padrao": True},
         {"chave": "relampago_pct", "tipo": "pct", "rotulo": "Desconto sugerido", "padrao": 30, "min": 5, "max": 90,
          "depende": "relampago_ativo"},
         {"chave": "relampago_horas", "tipo": "int", "rotulo": "Duração sugerida (horas)", "padrao": 6, "min": 1, "max": 72,
          "depende": "relampago_ativo"},
         {"chave": "relampago_avisar", "tipo": "bool", "rotulo": "Avisar os fãs no chat quando começar", "padrao": True,
          "depende": "relampago_ativo"},
     ]},
    {"id": "ppv", "secao": "Vender mais", "titulo": "Mensagem paga no privado (PPV)", "icone": "envelope",
     "descricao": "Ao criar um pack pago, dá para mandar a prévia embaçada direto no chat de um grupo de fãs ou de um fã só.",
     "campos": [
         {"chave": "ppv_ativo", "tipo": "bool", "rotulo": "Mostrar a opção ao criar pack", "padrao": True},
     ]},
    {"id": "perguntas", "secao": "Vender mais", "titulo": "Caixinha de perguntas paga", "icone": "pergunta",
     "descricao": "O fã paga para mandar uma pergunta. Você responde pelo Painel e a resposta vira post ou vai no privado.",
     "campos": [
         {"chave": "perguntas_ativo", "tipo": "bool", "rotulo": "Abrir a caixinha", "padrao": False},
         {"chave": "perguntas_preco", "tipo": "reais", "rotulo": "Preço por pergunta", "padrao": 1000, "min": 100,
          "max": 100000, "depende": "perguntas_ativo"},
         {"chave": "perguntas_resposta", "tipo": "escolha", "rotulo": "Onde sai a resposta", "padrao": "feed",
          "opcoes": [["feed", "Post no feed (VIP)"], ["privado", "Só no privado do fã"], ["fa_escolhe", "O fã escolhe"]],
          "depende": "perguntas_ativo"},
         {"chave": "perguntas_anonimo", "tipo": "bool", "rotulo": "No feed, não mostrar o nome de quem perguntou",
          "padrao": True, "depende": "perguntas_ativo"},
         {"chave": "perguntas_info", "tipo": "texto", "rotulo": "Recado na caixinha (opcional)", "padrao": "", "max": 140,
          "depende": "perguntas_ativo"},
     ],
     "acoes": [{"acao": "perguntas-painel", "rotulo": "Ver perguntas"}]},
    {"id": "carteira", "secao": "Vender mais", "titulo": "Carteira de créditos", "icone": "carteira",
     "descricao": "O fã recarrega um saldo uma vez e compra packs, VIP e mimos com um toque, sem gerar PIX a cada compra.",
     "campos": [
         {"chave": "carteira_ativo", "tipo": "bool", "rotulo": "Ativar a carteira", "padrao": False},
         {"chave": "carteira_valores", "tipo": "lista", "rotulo": "Valores de recarga (R$)", "padrao": "20,50,100,200",
          "min": 5, "max": 5000, "depende": "carteira_ativo"},
         {"chave": "carteira_bonus", "tipo": "pct", "rotulo": "Bônus na recarga", "padrao": 10, "min": 0, "max": 50,
          "depende": "carteira_ativo", "ajuda": "Ex: 10% → recarregou R$ 100, ganhou R$ 110. 0 desliga."},
         {"chave": "carteira_bonus_min", "tipo": "reais", "rotulo": "Bônus a partir de", "padrao": 5000, "min": 100,
          "max": 1000000, "depende": "carteira_ativo"},
     ]},
    # ── Engajar ─────────────────────────────────────────────────────────────
    {"id": "niveis", "secao": "Engajar os fãs", "titulo": "Níveis de fã", "icone": "medalha",
     "descricao": "Bronze, prata e ouro pelo quanto o fã já gastou, com selo e desconto automático nos packs.",
     "campos": [
         {"chave": "niveis_ativo", "tipo": "bool", "rotulo": "Ativar níveis", "padrao": False},
         {"chave": "nivel_prata", "tipo": "reais", "rotulo": "Prata a partir de", "padrao": 10000, "min": 100,
          "max": 10000000, "depende": "niveis_ativo"},
         {"chave": "nivel_ouro", "tipo": "reais", "rotulo": "Ouro a partir de", "padrao": 30000, "min": 100,
          "max": 10000000, "depende": "niveis_ativo"},
         {"chave": "desconto_prata", "tipo": "pct", "rotulo": "Desconto do prata", "padrao": 5, "min": 0, "max": 50,
          "depende": "niveis_ativo"},
         {"chave": "desconto_ouro", "tipo": "pct", "rotulo": "Desconto do ouro", "padrao": 10, "min": 0, "max": 50,
          "depende": "niveis_ativo"},
     ]},
    {"id": "comentarios", "secao": "Engajar os fãs", "titulo": "Comentários nos posts", "icone": "chat",
     "descricao": "Os fãs comentam nos posts e packs. Com a moderação ligada, só aparece o que você aprovar.",
     "campos": [
         {"chave": "comentarios_ativo", "tipo": "bool", "rotulo": "Permitir comentários", "padrao": False},
         {"chave": "comentarios_moderar", "tipo": "bool", "rotulo": "Aprovar antes de aparecer", "padrao": True,
          "depende": "comentarios_ativo"},
     ],
     "acoes": [{"acao": "comentarios-painel", "rotulo": "Moderar comentários"}]},
    {"id": "sorteios", "secao": "Engajar os fãs", "titulo": "Sorteios", "icone": "dado",
     "descricao": "Crie um sorteio, os fãs participam com um toque e o bot sorteia e avisa quem ganhou na hora marcada.",
     "campos": [
         {"chave": "sorteios_ativo", "tipo": "bool", "rotulo": "Ativar sorteios", "padrao": False},
     ],
     "acoes": [{"acao": "sorteios-painel", "rotulo": "Meus sorteios"}]},
    {"id": "novidades", "secao": "Engajar os fãs", "titulo": "Aviso de conteúdo novo", "icone": "sino",
     "descricao": "Quando sai um pack ou post novo, o bot avisa no chat. O fã pode silenciar em Minhas.",
     "campos": [
         {"chave": "novidades_ativo", "tipo": "bool", "rotulo": "Avisar sobre conteúdo novo", "padrao": False},
         {"chave": "novidades_publico", "tipo": "escolha", "rotulo": "Quem recebe", "padrao": "vip",
          "opcoes": [["vip", "Assinantes VIP"], ["compradores", "Quem já comprou"], ["todos", "Todos os fãs"]],
          "depende": "novidades_ativo"},
         {"chave": "novidades_posts", "tipo": "bool", "rotulo": "Avisar também dos posts grátis", "padrao": False,
          "depende": "novidades_ativo"},
     ]},
    {"id": "sequencia", "secao": "Engajar os fãs", "titulo": "Sequência de boas-vindas do VIP", "icone": "carta",
     "descricao": "Mensagens automáticas no 1º, 3º e 7º dia de assinatura. Deixe em branco o dia que não quiser.",
     "campos": [
         {"chave": "seq_ativo", "tipo": "bool", "rotulo": "Mandar a sequência", "padrao": False},
         {"chave": "seq_dia1", "tipo": "textarea", "rotulo": "1º dia", "depende": "seq_ativo", "max": 800,
          "padrao": "Oi {nome}! Que bom ter você no VIP 💖 Dá uma olhada nos packs da loja, tem coisa nova toda semana."},
         {"chave": "seq_dia3", "tipo": "textarea", "rotulo": "3º dia", "depende": "seq_ativo", "max": 800,
          "padrao": "{nome}, já viu os stories de hoje? Me conta o que você mais curtiu até agora 😘"},
         {"chave": "seq_dia7", "tipo": "textarea", "rotulo": "7º dia", "depende": "seq_ativo", "max": 800,
          "padrao": "Uma semana juntos! 🥰 Se quiser algo só seu, faz um pedido personalizado na loja."},
     ]},
    {"id": "aniversario", "secao": "Engajar os fãs", "titulo": "Aniversário de assinatura", "icone": "bolo",
     "descricao": "Quando o fã completa meses de VIP, recebe uma mensagem e um cupom de presente.",
     "campos": [
         {"chave": "aniv_ativo", "tipo": "bool", "rotulo": "Comemorar aniversários", "padrao": False},
         {"chave": "aniv_meses", "tipo": "lista", "rotulo": "Em quais meses", "padrao": "1,3,6,12", "min": 1, "max": 60,
          "depende": "aniv_ativo"},
         {"chave": "aniv_pct", "tipo": "pct", "rotulo": "Cupom de presente", "padrao": 15, "min": 0, "max": 90,
          "depende": "aniv_ativo", "ajuda": "0 manda só a mensagem."},
         {"chave": "aniv_msg", "tipo": "textarea", "rotulo": "Mensagem", "depende": "aniv_ativo", "max": 500,
          "padrao": "🎉 {meses} de VIP, {nome}! Obrigada por estar comigo. Seu presente: {desconto} em qualquer pack."},
     ]},
    {"id": "avaliacoes", "secao": "Engajar os fãs", "titulo": "Avaliação dos packs", "icone": "estrela",
     "descricao": "Quem comprou dá de 1 a 5 estrelas. A nota aparece na vitrine.",
     "campos": [
         {"chave": "aval_ativo", "tipo": "bool", "rotulo": "Pedir avaliação", "padrao": False},
         {"chave": "aval_min", "tipo": "int", "rotulo": "Mostrar a nota a partir de quantas avaliações", "padrao": 3,
          "min": 1, "max": 50, "depende": "aval_ativo"},
     ]},
    # ── Divulgação ──────────────────────────────────────────────────────────
    {"id": "links", "secao": "Divulgação e números", "titulo": "Links de divulgação", "icone": "link",
     "descricao": "Um link para cada rede social (Instagram, TikTok, X...). O painel mostra quantos fãs e vendas cada um trouxe. "
                  "Cada link pode aplicar um cupom sozinho.",
     "campos": [
         {"chave": "links_cupom_dias", "tipo": "int", "rotulo": "O cupom do link vale por (dias) para o fã", "padrao": 7,
          "min": 1, "max": 90},
     ],
     "acoes": [{"acao": "links-painel", "rotulo": "Meus links"}]},
    {"id": "indicacao", "secao": "Divulgação e números", "titulo": "Programa de indicação", "icone": "usuarios",
     "descricao": "Cada fã tem um link para indicar amigos. Quando o amigo paga, quem indicou ganha crédito na carteira "
                  "(ou um cupom, se a carteira estiver desligada).",
     "campos": [
         {"chave": "indic_ativo", "tipo": "bool", "rotulo": "Ativar indicações", "padrao": False},
         {"chave": "indic_recompensa", "tipo": "reais", "rotulo": "Recompensa por amigo", "padrao": 1000, "min": 100,
          "max": 100000, "depende": "indic_ativo"},
         {"chave": "indic_quando", "tipo": "escolha", "rotulo": "Vale quando o amigo", "padrao": "compra",
          "opcoes": [["compra", "Faz a 1ª compra"], ["vip", "Assina o VIP"]], "depende": "indic_ativo"},
         {"chave": "indic_amigo_pct", "tipo": "pct", "rotulo": "Desconto para o amigo na 1ª compra", "padrao": 0,
          "min": 0, "max": 50, "depende": "indic_ativo"},
     ]},
    {"id": "relatorio", "secao": "Divulgação e números", "titulo": "Relatório mensal", "icone": "planilha",
     "descricao": "Planilha (CSV) com todas as vendas, mimos e assinaturas do mês, para a contabilidade.",
     "campos": [
         {"chave": "relat_auto", "tipo": "bool", "rotulo": "Mandar no chat todo dia 1º", "padrao": False},
     ],
     "acoes": [{"acao": "relatorio", "rotulo": "Receber um relatório agora"}]},
    # ── Proteção ────────────────────────────────────────────────────────────
    {"id": "vazamento", "secao": "Proteção e organização", "titulo": "Caçar vazamento", "icone": "lupa-id",
     "descricao": "Digite o ID que aparece na marca d'água e veja quem é, o que comprou, e bloqueie na hora.",
     "campos": [
         {"chave": "vaz_revogar", "tipo": "bool", "rotulo": "Ao bloquear, cortar também as compras e o VIP", "padrao": True},
     ],
     "acoes": [{"acao": "vazamento", "rotulo": "Procurar um ID"}]},
    {"id": "teaser", "secao": "Proteção e organização", "titulo": "Teaser em vídeo", "icone": "video",
     "descricao": "Nos packs com vídeo, uma prévia curta borrada toca sozinha no card, no lugar da foto embaçada.",
     "campos": [
         {"chave": "teaser_ativo", "tipo": "bool", "rotulo": "Mostrar teaser em vídeo", "padrao": False},
         {"chave": "teaser_segundos", "tipo": "int", "rotulo": "Duração (segundos)", "padrao": 3, "min": 2, "max": 6,
          "depende": "teaser_ativo"},
         {"chave": "teaser_blur", "tipo": "int", "rotulo": "Desfoque (quanto maior, menos se vê)", "padrao": 18, "min": 6,
          "max": 40, "depende": "teaser_ativo"},
     ]},
    {"id": "backup_midias", "secao": "Proteção e organização", "titulo": "Backup das mídias", "icone": "nuvem",
     "descricao": "Copia todas as fotos e vídeos para fora do Telegram, para não perder nada se o bot ou o canal cair. "
                  "Arquivos acima de 20 MB não podem ser baixados pelo bot e ficam de fora.",
     "campos": [
         {"chave": "bkm_ativo", "tipo": "bool", "rotulo": "Fazer backup todo dia", "padrao": False},
         {"chave": "bkm_destino", "tipo": "escolha", "rotulo": "Onde guardar", "padrao": "volume",
          "opcoes": [["volume", "Disco do servidor (/data)"], ["s3", "Bucket S3 (Railway, R2, AWS...)"]], "depende": "bkm_ativo"},
         {"chave": "bkm_s3_endpoint", "tipo": "texto", "rotulo": "Endpoint do bucket", "padrao": "", "max": 200,
          "depende": "bkm_ativo", "ajuda": "Ex: https://storage.railway.app (só para S3)"},
         {"chave": "bkm_s3_bucket", "tipo": "texto", "rotulo": "Nome do bucket", "padrao": "", "max": 100, "depende": "bkm_ativo"},
         {"chave": "bkm_s3_regiao", "tipo": "texto", "rotulo": "Região", "padrao": "auto", "max": 40, "depende": "bkm_ativo"},
         {"chave": "bkm_s3_chave", "tipo": "texto", "rotulo": "Access key", "padrao": "", "max": 200, "depende": "bkm_ativo"},
         {"chave": "bkm_s3_segredo", "tipo": "segredo", "rotulo": "Secret key", "padrao": "", "max": 200, "depende": "bkm_ativo"},
     ],
     "acoes": [{"acao": "backup-agora", "rotulo": "Fazer backup agora"}]},
    {"id": "colecoes", "secao": "Proteção e organização", "titulo": "Coleções", "icone": "pasta",
     "descricao": "Agrupe os packs em pastas na vitrine (ex: Praia, Bastidores). Os fãs filtram por coleção.",
     "campos": [
         {"chave": "colecoes_ativo", "tipo": "bool", "rotulo": "Mostrar coleções na vitrine", "padrao": False},
     ],
     "acoes": [{"acao": "colecoes-painel", "rotulo": "Organizar coleções"}]},
    {"id": "tema", "secao": "Proteção e organização", "titulo": "Cores da loja", "icone": "paleta",
     "descricao": "A cor principal da loja e o modo claro ou escuro.",
     "campos": [
         {"chave": "tema_cor", "tipo": "escolha", "rotulo": "Cor", "padrao": "rosa",
          "opcoes": [["rosa", "Rosa"], ["roxo", "Roxo"], ["azul", "Azul"], ["verde", "Verde"], ["laranja", "Laranja"],
                     ["vermelho", "Vermelho"], ["dourado", "Dourado"]]},
         {"chave": "tema_modo", "tipo": "escolha", "rotulo": "Modo", "padrao": "auto",
          "opcoes": [["auto", "Igual ao Telegram"], ["escuro", "Escuro"], ["claro", "Claro"]]},
     ]},
]
CAMPOS = {c["chave"]: c for g in GRUPOS for c in g["campos"]}


def _converter(campo: dict, bruto: str | None):
    if bruto is None:
        return campo["padrao"]
    t = campo["tipo"]
    if t == "bool":
        return bruto == "1"
    if t in ("int", "pct", "reais"):
        try:
            return int(bruto)
        except ValueError:
            return campo["padrao"]
    return bruto


async def valor(db: Database, chave: str):
    campo = CAMPOS[chave]
    return _converter(campo, await db.get_ajuste(chave))


async def lista(db: Database, chave: str) -> list[int]:
    return [int(x) for x in str(await valor(db, chave)).split(",") if x.strip().isdigit()]


async def varios(db: Database, *chaves: str) -> dict:
    return {k: await valor(db, k) for k in chaves}


async def para_app(db: Database) -> list[dict]:
    grupos = []
    for g in GRUPOS:
        campos = []
        for c in g["campos"]:
            v = await valor(db, c["chave"])
            d = {k: c[k] for k in ("chave", "tipo", "rotulo", "ajuda", "opcoes", "min", "max", "depende") if k in c}
            if c["tipo"] == "segredo":
                d["preenchido"], v = bool(v), ""
            elif c["tipo"] == "reais":
                d["fmt"] = fmt_reais(v)
            d["valor"] = v
            campos.append(d)
        grupos.append({k: g[k] for k in ("id", "secao", "titulo", "icone", "descricao") } |
                      {"campos": campos, "acoes": g.get("acoes", []),
                       "ligado": next((c["valor"] for c in campos if c["tipo"] == "bool"), None)})
    return grupos


def validar(chave: str, valor_bruto) -> tuple[str | None, str | None]:
    """(valor para o banco, erro)."""
    campo = CAMPOS.get(chave)
    if not campo:
        return None, "Ajuste desconhecido."
    t, nome = campo["tipo"], campo["rotulo"]
    if t == "bool":
        return ("1" if valor_bruto in (True, 1, "1", "true", "on") else "0"), None
    if t in ("int", "pct"):
        try:
            n = int(str(valor_bruto).replace("%", "").strip())
        except ValueError:
            return None, f"{nome}: número inválido."
        if not campo.get("min", 0) <= n <= campo.get("max", 10 ** 9):
            return None, f"{nome}: use de {campo.get('min', 0)} a {campo.get('max')}."
        return str(n), None
    if t == "reais":
        n = valor_bruto if isinstance(valor_bruto, int) else parse_preco(str(valor_bruto))
        if not n or not campo.get("min", 1) <= n <= campo.get("max", 10 ** 9):
            return None, f"{nome}: valor inválido (de {fmt_reais(campo.get('min', 100))} a {fmt_reais(campo.get('max', 10 ** 9))})."
        return str(n), None
    if t == "escolha":
        if valor_bruto not in [o[0] for o in campo["opcoes"]]:
            return None, f"{nome}: opção inválida."
        return valor_bruto, None
    if t == "lista":
        nums = sorted({int(x) for x in str(valor_bruto).replace(";", ",").split(",") if x.strip().isdigit()})
        nums = [n for n in nums if campo.get("min", 0) <= n <= campo.get("max", 10 ** 9)][:10]
        if not nums:
            return None, f"{nome}: informe pelo menos um número, separados por vírgula."
        return ",".join(map(str, nums)), None
    texto = str(valor_bruto or "").strip()
    if t == "texto" or t == "segredo":
        texto = " ".join(texto.split())
    return texto[:campo.get("max", 500)], None


# ── Rotas ─────────────────────────────────────────────────────────────────
async def ver(request: web.Request) -> web.Response:
    from criadora import _admin
    _admin(request, "ajustes")
    return web.json_response({"ok": True, "grupos": await para_app(request.app["db"])})


async def salvar(request: web.Request) -> web.Response:
    from criadora import _admin
    _admin(request, "ajustes")
    db: Database = request.app["db"]
    corpo = await request.json()
    if not isinstance(corpo, dict) or not corpo:
        return web.json_response({"ok": False, "erro": "Nada para salvar."}, status=400)
    prontos = {}
    for chave, v in corpo.items():
        if CAMPOS.get(chave, {}).get("tipo") == "segredo" and v in ("", None):
            continue                       # segredo em branco = manter o que já está salvo
        bruto, erro = validar(chave, v)
        if erro:
            return web.json_response({"ok": False, "erro": erro}, status=400)
        prontos[chave] = bruto
    if "nivel_prata" in prontos or "nivel_ouro" in prontos:
        prata = int(prontos.get("nivel_prata") or await valor(db, "nivel_prata"))
        ouro = int(prontos.get("nivel_ouro") or await valor(db, "nivel_ouro"))
        if ouro <= prata:
            return web.json_response({"ok": False, "erro": "O ouro precisa começar num valor maior que o prata."}, status=400)
    for chave, v in prontos.items():
        await db.set_ajuste(chave, v)
    return web.json_response({"ok": True, "grupos": await para_app(db)})


def registrar_rotas(app: web.Application) -> None:
    app.router.add_get("/api/criadora/recursos", ver)
    app.router.add_post("/api/criadora/recursos", salvar)
