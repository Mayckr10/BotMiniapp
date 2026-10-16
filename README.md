# Bot de Vendas de Conteúdo (Telegram + PIX)

Bot em **aiogram 3.x** no estilo **Privacy** para uma criadora: assinatura VIP com canal privado,
pay per view (PPV), packs avulsos, combos, gorjetas e uma **loja Mini App** dentro do Telegram.

Fluxo dos packs:
a criadora cria o pack no privado, o bot gera as amostras com **blur (Pillow/FFmpeg)**,
publica no **Canal de Amostras** com um botão de *deep link*. O cliente paga via **PIX**,
e o **webhook** do gateway libera as mídias originais com `protect_content=True`.

Inclui um **simulador local do Telegram** (interface web) para testar tudo sem token real
e sem precisar entrar em nenhuma conta.

## Estrutura

```
bot-telegram/
├── main.py              # sobe o bot (polling) + servidor do webhook PIX
├── config.py            # lê o .env
├── database.py          # SQLite (aiosqlite): tabelas packs e vendas
├── blur_service.py      # GaussianBlur(radius=20) com Pillow → BytesIO; teaser de vídeo via FFmpeg
├── admin_handlers.py    # /novo_pack (FSM), prévia com blur, postagem no canal
├── client_handlers.py   # /start pack_xxxx → cobrança PIX + QR Code; botão "Já paguei"
├── webhook.py           # POST /webhook/pix → valida, marca PAGO e entrega as mídias originais
├── admin_vendas.py      # cupons, combos, /stats, /broadcast, /reenviar
├── remarketing.py       # job: lembrete do PIX, expiração e cupom pessoal depois
├── backup.py            # backup diário do SQLite enviado no privado da criadora
├── assinaturas.py       # planos VIP, aprovação de entrada no canal VIP, avisos e remoção no vencimento
├── vendas.py            # cria o pedido (preço, cupom, PIX), usado pelo bot e pela loja
├── envio.py             # envio em massa respeitando o limite do Telegram (broadcast, PPV)
├── loja.py              # API do Mini App (loja dentro do Telegram)
├── miniapp/             # página da loja (index.html)
├── Dockerfile           # para Railway, Fly.io ou VPS com Docker
├── deploy/              # exemplos de systemd e Caddy (HTTPS) para VPS
├── payments.py          # gateways PIX: Mock (testes) e Pushin Pay (real), mesma interface
├── rodar_simulador.py   # roda o simulador + o bot com um único comando
├── simulador/           # falsa Bot API do Telegram + interface web
├── requirements.txt
└── .env.example
```

## 1. Testar no simulador (sem token, sem conta)

```bash
pip install -r requirements.txt
python rodar_simulador.py
```

Abra **http://localhost:8081**. A tela tem 4 colunas: chat da Criadora, Canal de Amostras, chat do Cliente e o Gateway PIX (mock).

1. No chat da Criadora: `/novo_pack`, envie fotos/vídeos pelo 📎 (selecione vários para formar um álbum) e toque em **✅ Concluir envio**.
2. Envie o título, depois o preço (`29,90`), e toque em **📢 Publicar no canal**.
3. No Canal, toque em **🔓 Liberar Conteúdo** (equivale a abrir `t.me/Bot?start=pack_xxxx`). Na primeira vez o Cliente confirma ter 18+, e em seguida recebe o QR Code e o PIX Copia e Cola.
4. No painel Gateway, toque em **💸 Pagar**. O mock chama o `/webhook/pix` do bot e o cliente recebe as mídias originais marcadas como protegidas.

5. **VIP:** no Cliente, `/assinar`, escolha um plano, pague no Gateway e toque em **🔐 Entrar no canal VIP**. A aba 🔒 Canal VIP mostra a entrada aprovada.
6. **Loja (Mini App):** no Cliente, toque no botão **🛍️ Loja** (embaixo) ou em "Abrir a loja" no perfil. Escolha um pack, pague no Gateway e o conteúdo chega no chat.

O simulador não é um mock dos handlers: o `main.py` roda sem nenhuma alteração e só troca o
endereço da Bot API (`TELEGRAM_API_BASE`) para o servidor local. O que funciona ali funciona no Telegram.

## Ferramentas de vendas (Fase 2)

Todos os comandos abaixo são do chat privado da criadora. `/admin` mostra a lista.

| Comando | O que faz |
|---|---|
| `/cupom VERAO10 10%` | Cupom percentual. Também aceita valor fixo (`/cupom FIXO5 5,00`) e os opcionais `pack_xxxx`/`combo_xxxx` (só vale para ele), `usos=50` e `dias=7`. |
| `/cupons`, `/cupom_off CODIGO` | Lista e desativa cupons. |
| `/novo_combo` | Escolhe 2 ou mais packs, define título e preço e publica no canal com o botão "Liberar Combo". |
| `/combos` | Lista combos e links. |
| `/stats` | Hoje, 7 e 30 dias: faturamento, nº de vendas, ticket médio, aberturas de link, conversão, vendas com cupom, novos clientes, item mais vendido e PIX pendentes. |
| `/broadcast` ou `/broadcast pack_xxxx` | Copia a próxima mensagem (texto, foto ou vídeo) para todos os compradores, ou só para os de um pack. Pede confirmação. |
| `/reenviar USER_ID pack_xxxx` | Libera e reenvia um pack manualmente. |

**Cupom pelo link:** o cliente abre `t.me/SeuBot?start=pack_8f3a_VERAO10` (ou `combo_xxxx_VERAO10`). O bot aplica o desconto e mostra "de ~~R$ 29,90~~ por R$ 26,91". Um cupom inválido, expirado ou esgotado não impede a compra: o bot avisa o motivo e cobra o preço normal. O uso do cupom só é contado quando o PIX é pago.

**Remarketing de PIX pendente** (`remarketing.py`, roda sozinho junto com o bot):
1. Depois de `LEMBRETE_MINUTOS` (30), o cliente recebe o mesmo PIX Copia e Cola e o botão "Já paguei".
2. Depois de `CUPOM_REMARKETING_MINUTOS` (24 h), o bot cria um cupom pessoal: só aquele cliente, só aquele item, 1 uso, `CUPOM_REMARKETING_PCT` (15%) de desconto e validade de `CUPOM_REMARKETING_VALIDADE_HORAS` (48 h). O cliente recebe um botão com o link do desconto.

Cada venda recebe no máximo um lembrete e um cupom. Ninguém recebe remarketing se já tem o conteúdo, se gerou um PIX mais novo do mesmo item ou se a venda já tinha cupom. No simulador esses tempos ficam em ~1 min (lembrete), ~2 min (PIX expira) e ~3 min (cupom) para dar para ver acontecendo.

**Broadcast e limite do Telegram:** o envio fica abaixo de `BROADCAST_POR_SEGUNDO` (25/s). Quando o Telegram responde 429 (`TelegramRetryAfter`), o bot espera o tempo pedido e tenta de novo. Quem bloqueou o bot (403) é marcado e sai dos próximos envios. No fim, a criadora recebe o resumo: enviados, bloquearam e falhas. O envio roda em segundo plano, sem travar o bot.

**Cliente:** `/meus_packs` lista o que ele comprou, com botões para receber de novo. Comprar um combo libera cada pack dele, e abrir o link de algo que já comprou reenvia sem cobrar.

**Banco antigo:** um `bot.db` criado pela versão do MVP é atualizado sozinho na inicialização (colunas novas e acessos das vendas já pagas).

## Pronto para vender (Fase 3)

**Confirmação 18+.** Na primeira vez que alguém abre um link de venda, o bot pergunta se a pessoa tem
18 anos ou mais antes de mostrar o PIX. "Sim" fica salvo no banco (`usuarios.maior_idade_em`) e a compra
continua de onde parou. "Sou menor" bloqueia as compras daquela conta. Admins não passam pela pergunta.
`TERMOS_URL` coloca um link para os seus termos na mesma mensagem, e `EXIGIR_MAIORIDADE=0` desliga a confirmação.

**PIX com validade.** Cada cobrança vale `PIX_VALIDADE_MINUTOS` (60). A validade aparece na mensagem do
PIX. Quando vence, a venda vira `EXPIRADO` e o cliente recebe, sem notificação sonora, o botão
**🔄 Gerar novo PIX**, que mantém o mesmo cupom se ele ainda for válido. Se o dinheiro cair depois do vencimento,
o bot entrega do mesmo jeito: um pagamento confirmado pelo gateway nunca é perdido.

**"Já paguei".** O botão consulta o gateway na hora, para o caso de o webhook atrasar. Num PIX vencido e
não pago, ele avisa e oferece um novo código.

**Backup automático.** Todo dia na `BACKUP_HORA` (4h), o bot faz uma cópia consistente do banco com o bot
rodando, compacta (`.db.gz`) e envia como arquivo no privado de cada admin. `/backup` envia na hora.
Para restaurar: pare o bot, `gunzip backup-AAAA-MM-DD_HHMM.db.gz`, renomeie o arquivo para o nome do
`DB_PATH` (ex: `bot.db`) e inicie de novo.

## Modo Privacy (uma criadora)

Tudo que é da criadora fica no `.env` e em comandos do bot, nada no código. Para usar com **outra criadora**,
copie a pasta, troque o `.env` (token do bot dela, `ADMIN_IDS`, canais, `CRIADORA_NOME`) e rode outra instância.
Cada criadora tem o próprio bot e o próprio banco.

**Perfil.** O `/start` sem link mostra a foto, a bio e os botões: ⭐ Assinar VIP, 📦 Packs, 💝 Mimo, 🗂️ Meus conteúdos
e 🛍️ Abrir a loja. A criadora define foto e bio com `/perfil`.

**Assinatura VIP.** Os planos padrão vêm de `PLANOS_PADRAO` (Mensal, Trimestral, Semestral) e aparecem com a % de
economia em relação ao mensal. Depois do PIX pago, o cliente recebe o botão do canal VIP. O link pede aprovação,
e o bot só aprova quem tem assinatura ativa: se o link vazar, quem não assina é recusado e recebe os planos.
`ASSINATURA_AVISO_DIAS` (3) antes de vencer, o assinante recebe o aviso para renovar (os dias são somados).
No vencimento, o bot remove a pessoa do canal e manda os planos de novo.

**Pay per view.** `/ppv` funciona como o `/novo_pack`, mas no final envia a prévia embaçada direto no privado
dos assinantes (ou de todos os clientes), com o botão **🔓 Desbloquear · R$**. O PPV não aparece no catálogo.

**Gorjetas (mimos).** `/gorjeta` ou o botão 💝 mostram os valores de `GORJETA_VALORES` ou aceitam um valor livre.
A criadora é avisada na hora.

**Sem chat.** Mensagens livres que o fã manda para o bot recebem o menu com os botões de compra.

| Comando (criadora) | O que faz |
|---|---|
| `/perfil` | Foto e bio do perfil. |
| `/planos`, `/plano Nome dias preço`, `/plano_off ID` | Lista, cria e desativa planos VIP. |
| `/assinantes` | Assinantes ativos e vencimentos. |
| `/dar_vip USER_ID dias`, `/remover_vip USER_ID` | Cortesia e remoção manual. |
| `/ppv` | Novo conteúdo pay per view enviado no privado. |

Comandos do fã: `/start`, `/assinar`, `/assinatura`, `/gorjeta`, `/meus_packs`.

**Canal VIP de verdade:** crie um canal **privado**, adicione o bot como administrador com permissão de
**convidar usuários** e **banir usuários**, e coloque o ID em `VIP_CHANNEL_ID`.

## Loja dentro do Telegram (Mini App)

A loja abre dentro do Telegram (botão **🛍️ Loja** ao lado do campo de mensagem e "Abrir a loja" no perfil),
com abas Início, VIP, Minhas e Mimos. O cliente paga o PIX na própria página, e assim que o pagamento é
confirmado o **bot manda o conteúdo no chat**, igual à compra pelo bot.

- Precisa de `PUBLIC_BASE_URL` com **HTTPS** (o Telegram não abre Mini App em http). O endereço é `https://seu-dominio/loja`, e o bot configura o botão de menu sozinho ao iniciar. `LOJA_ATIVA=0` desliga.
- Não tem login: cada chamada leva o `initData` do Telegram, que o servidor confere com o token do bot (HMAC). Ninguém consegue comprar ou ver compras como outra pessoa.
- A página só recebe as **prévias embaçadas**. As mídias originais nunca passam pela loja: quem entrega é o bot, com `protect_content`.
- Cupons, 18+, validade do PIX, "Já paguei" e remarketing funcionam igual ao bot.

### Modo criadora (painel no Mini App)

Quem está em `ADMIN_IDS` escolhe ao abrir a loja se entra como **Criadora** ou **Cliente** (dá para trocar depois).
No modo criadora o controle todo fica no Mini App, e os comandos do bot ficam só para emergência:

- **Painel**: vendas de hoje, da semana e do mês, assinantes, e estatísticas por período (faturamento, ticket médio, conversão, novos fãs, curtidas, PIX), gráfico de 14 dias, de onde vem a receita (packs, VIP, mimos) e os mais vendidos.
- **Curtidas e mimos**: os fãs curtem e mandam mimos em cada post ou pack; a criadora vê quantos mimos e quanto cada um rendeu.
- **Criar**: post de texto (com foto opcional) e pack pago com legenda, enviado do celular (até 30 arquivos, vídeos até 49 MB; maiores pelo `/novo_pack`). Pode postar a prévia no canal de amostras.
- **⋯** em cada post ou pack: editar (título, preço, legenda) ou apagar. Pack apagado sai da loja, mas quem comprou continua com acesso.
- **Perfil**: foto, nome e bio.
- **Aba Ajustes** (só para a criadora, que não vê as abas de compra): perfil, mensagens do bot, planos do VIP (nome, duração e preço; criar e remover), valores dos mimos, cupons de desconto (% ou R$, limite de usos e validade), revisão antes de publicar, prévia no canal por padrão e "ver a loja como fã".
- **Fila de revisão** (opcional, em Ajustes): com "Revisar antes de publicar" ligado, posts e packs novos ficam numa fila. Nada aparece para os fãs nem vai para o canal até a criadora tocar em Publicar (dá para ver como o fã vai ver, editar ou descartar).
- **Editor de fotos**: antes de publicar, o ✎ em cada foto abre cortar (livre, 1:1, 4:5, 9:16, 16:9), girar, espelhar, brilho, contraste, cor e filtros. A foto de perfil abre o editor com recorte redondo.
- **Mensagens do bot**: boas-vindas do /start (com foto), mensagem depois da compra e boas-vindas do VIP. Marcadores: `{nome}`, `{criadora}`, `{item}`.
- **Stories**: foto ou vídeo que fica 24h. Toque no avatar › Novo story (ou Painel › Criar). Os fãs veem o anel colorido no avatar (cinza depois de assistir), com barras de progresso, tocar para avançar e segurar para pausar. A criadora vê quem visualizou cada story e pode apagar.
- **Loja só para assinantes VIP**: quem não assina não vê nenhum conteúdo na loja (nem posts grátis, stories, packs, combos ou lançamentos). O servidor nem manda esses itens: o fã vê só uma tela com a quantidade do que existe e o botão de assinar. O que ele já comprou continua em Minhas. As fotos e vídeos dos posts saem com link assinado por pessoa e o servidor confere o VIP a cada acesso, então um link copiado não abre para quem não assina (nem depois que o VIP vence).
- **Marca d'água em fotos e vídeos**: na galeria de compras, a foto e o vídeo levam o @ e o ID de quem comprou. No vídeo a marca é gravada nos quadros com FFmpeg (baixar o arquivo não tira). Cada vídeo marcado fica num cache em disco por comprador; a marcação começa quando o fã abre o pack e o primeiro play espera alguns segundos. A criadora pode desligar em Ajustes.
- **Ícones**: a loja usa ícones SVG desenhados à mão (`ICONES` e `ic()` no `miniapp/index.html`), sem emojis, com a cor do tema.
- **Top fãs** (Painel): quem mais gastou nos últimos 30 dias ou desde o início, com a ficha de cada fã. O fã que está no top 10 vê a própria posição em Minhas (sem ver os outros).
- **Pedido personalizado** (Ajustes › Pedidos personalizados): a criadora liga, define o preço mínimo, o prazo e as regras. O fã descreve o pedido, oferece um valor e paga o PIX. A criadora recebe o aviso no chat e entrega pelo Painel › Pedidos (fotos e vídeos vão só para aquele fã, no chat e em Minhas compras) ou recusa com um recado. O bot não faz estorno automático: ao recusar, devolva o valor pela sua conta de pagamento.
- **Carrinho**: o fã junta vários packs e paga um PIX só. Em Ajustes › Carrinho dá para dar um desconto (ex: 10% levando 3 ou mais).
- **Meta de mimos** (Ajustes › Meta de mimos): título e valor; a barra aparece no feed e na aba Mimos. Todo mimo pago depois que a meta é criada soma. Quando bate, a criadora e quem ajudou recebem um aviso.
- **Busca e filtros**: o fã busca por título, legenda ou texto dos posts e filtra por vídeo/fotos, comprados/não comprados, e ordena por recentes, curtidos ou preço.

## Testar no Telegram só pelo celular

Sem PC, o bot precisa rodar num servidor na nuvem. O caminho mais simples pelo navegador do celular é o **Railway**:

1. **Bot:** no Telegram, fale com o **@BotFather**, mande `/newbot` e copie o token.
2. **Canais:** crie o Canal de Amostras e o Canal VIP (privado) e adicione o bot como administrador nos dois (postar, convidar e banir usuários).
3. **IDs:** mande qualquer mensagem para o **@userinfobot** (o seu ID) e encaminhe um post de cada canal para ele (IDs começando com `-100`).
4. **Código no GitHub:** crie um repositório privado vazio no github.com e suba o projeto nele.
5. **Railway:** entre em railway.com com o GitHub, **New Project → Deploy from GitHub repo** e escolha o repositório.
   Em **Variables**: `BOT_TOKEN`, `ADMIN_IDS` (seu ID), `CHANNEL_ID`, `VIP_CHANNEL_ID`, `PIX_GATEWAY=mock`,
   `WEBHOOK_SECRET` (qualquer texto longo) e `CRIADORA_NOME`. Em **Settings → Networking**, gere um domínio e coloque
   em `PUBLIC_BASE_URL` (`https://xxx.up.railway.app`). Crie um **Volume** montado em `/data`.
6. **Teste:** mande `/start` para o bot. Você é a criadora (`/novo_pack`, `/ppv`, `/perfil`...) e também pode comprar.

Com `PIX_GATEWAY=mock`, a mensagem do PIX e a loja mostram o botão **🧪 Simular pagamento (teste)**, que
confirma o pagamento na hora sem dinheiro. Para ver como cliente, peça para outra pessoa abrir o bot.
Para cobrar de verdade, troque para `PIX_GATEWAY=pushinpay` (o botão de teste some sozinho).

## 2. Rodar de verdade

1. Crie o bot no **@BotFather** (`/newbot`) e copie o token.
2. Crie o **Canal de Amostras** e adicione o bot como **administrador** com permissão de postar.
3. Descubra os IDs: o seu (fale com o **@userinfobot**) e o do canal (encaminhe um post do canal para o @userinfobot; o ID começa com `-100`).
4. Copie `.env.example` para `.env` e preencha `BOT_TOKEN`, `ADMIN_IDS` e `CHANNEL_ID`. Deixe `TELEGRAM_API_BASE` vazio.
5. Abra uma conversa com o seu bot e mande `/start` (o bot só consegue mandar mensagens para quem já falou com ele).
6. `python main.py`

Nesse ponto já dá para testar em produção com `PIX_GATEWAY=mock`: pague a cobrança com
`curl -X POST http://localhost:8080/mock/pagar/<pix_id>` (a lista está em `GET /mock/cobrancas`).

### Ligando um gateway PIX real

1. Hospede o bot com HTTPS (próxima seção), porque o gateway precisa alcançar o webhook pela internet.
2. No `.env`: `PUBLIC_BASE_URL=https://seu-dominio`, `WEBHOOK_SECRET=<algo longo e aleatório>`,
   `PIX_GATEWAY=pushinpay` e `PUSHINPAY_TOKEN=<seu token>`.
   Gere o segredo com `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. O bot envia o webhook como `https://seu-dominio/webhook/pix?token=<WEBHOOK_SECRET>` ao criar cada cobrança.
4. Faça uma compra real de R$ 1,00 com um pack de teste antes de divulgar.

**Sem mexer em variáveis:** a criadora cola o token da conta Pushin Pay dela em *Ajustes › Vendas › Recebimento*
(o token é conferido na Pushin Pay e nunca volta para o app; só os 4 últimos dígitos aparecem). Assim que ela
conecta, o modo teste desliga sozinho.

**Mercado Pago (marketplace, recomendado):**
1. O dono entra em mercadopago.com.br/developers com a conta Mercado Pago **dele**, cria uma aplicação
   (Pagamentos online › CheckoutAPI) e em *URLs de redirecionamento* cadastra `https://seu-dominio/mp/retorno`.
2. No Railway: `MP_CLIENT_ID` e `MP_CLIENT_SECRET` com o Client ID e o Client Secret da aplicação
   (só no ambiente: o banco vai para a criadora no backup diário).
3. A criadora toca em *Ajustes › Vendas › Recebimento › Conectar Mercado Pago*, entra na conta dela e autoriza.
   Cada PIX é criado na conta dela com `application_fee` = a % do `/split`, que cai na conta dona da aplicação.
   O token dela é renovado sozinho antes de vencer (180 dias).

**Split do dono da plataforma:** quem está em `DONO_IDS` manda `/split 10` (Mercado Pago) ou
`/split <account_id> 10` (Pushin Pay, com o ID da conta Pushin Pay dele) para receber essa % de cada venda. `/split` mostra o
que está valendo e `/split off` desliga. A % vai de 1 a 50 (limite da Pushin Pay) e vale no lugar de
`PLATAFORMA_ACCOUNT_ID` / `PLATAFORMA_PCT`.

A classe `PushinPayGateway` em `payments.py` segue a documentação pública da Pushin Pay; **confira os
nomes dos campos na documentação atual antes de ir para produção**. Para Asaas, OpenPix ou Mercado Pago,
crie outra classe com os mesmos 3 métodos (`criar_cobranca`, `consultar_pago`, `ler_webhook`) e registre em `criar_gateway()`.

## 3. Hospedagem com HTTPS

Regras que valem para qualquer opção:
- **Rode uma única instância.** Duas cópias do bot disputam as mensagens do Telegram (erro 409) e o SQLite.
- **O banco precisa de disco persistente.** Sem volume, o `bot.db` some a cada deploy.
- Teste o webhook com `curl https://seu-dominio/health`, que deve responder `{"ok": true}`.

### Opção A: VPS (recomendada: mais barata e com disco persistente)

```bash
# Ubuntu 22.04/24.04, como root
adduser --disabled-password bot
apt install -y python3-venv ffmpeg caddy
su - bot -c "git clone <seu-repo> bot-telegram || true"   # ou envie o zip com scp e descompacte
su - bot -c "cd bot-telegram && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
# crie /home/bot/bot-telegram/.env (WEBHOOK_HOST=127.0.0.1, PUBLIC_BASE_URL=https://pagamentos.seudominio.com.br)
cp /home/bot/bot-telegram/deploy/bot-telegram.service /etc/systemd/system/
cp /home/bot/bot-telegram/deploy/Caddyfile /etc/caddy/Caddyfile        # edite o domínio
systemctl daemon-reload && systemctl enable --now bot-telegram && systemctl reload caddy
```

Aponte o domínio (registro A) para o IP da VPS. O Caddy emite e renova o certificado HTTPS sozinho.

**Sem domínio ou sem abrir portas:** use um *Cloudflare Tunnel* nomeado (`cloudflared tunnel create bot`,
`cloudflared tunnel route dns bot pagamentos.seudominio.com.br`, `cloudflared tunnel run --url http://localhost:8080 bot`).
O túnel rápido (`cloudflared tunnel --url http://localhost:8080`) troca de endereço a cada reinício,
então serve só para testes no seu PC.

### Opção B: Railway

1. Suba o projeto num repositório GitHub e crie um projeto no Railway a partir dele (o `Dockerfile` é usado automaticamente).
2. Em **Variables**, cadastre as mesmas chaves do `.env`.
3. Crie um **Volume** para o serviço, montado em `/data`. O `DB_PATH` já aponta para `/data/bot.db`.
4. Gere um domínio público para o serviço e use-o em `PUBLIC_BASE_URL` (`https://xxx.up.railway.app`).
5. Mantenha **1 réplica**.

### Opção C: Fly.io

```bash
fly launch --no-deploy                 # detecta o Dockerfile
fly volumes create dados --size 1      # 1 GB para o SQLite
fly secrets set BOT_TOKEN=... ADMIN_IDS=... CHANNEL_ID=... WEBHOOK_SECRET=... PUBLIC_BASE_URL=https://<app>.fly.dev
fly deploy
fly scale count 1
```

No `fly.toml`, monte o volume e deixe a máquina sempre ligada (o bot usa polling):

```toml
[mounts]
  source = "dados"
  destination = "/data"

[http_service]
  internal_port = 8080
  force_https = true
  auto_stop_machines = "off"
  min_machines_running = 1
```

## Segurança do pagamento

- O webhook exige o segredo (`X-Webhook-Secret` ou `?token=`); sem ele, responde 401.
- Antes de entregar, o bot **reconsulta o status na API do gateway** em vez de confiar no corpo do POST.
- `marcar_pago()` é atômico: se o gateway reenviar o webhook, o pack não é entregue duas vezes.
- O botão "Já paguei" usa a mesma rotina, para o caso de o webhook atrasar.
- PIX vencido que for pago mesmo assim é entregue (`EXPIRADO` → `PAGO`).
- Quem já comprou e abre o link de novo recebe o conteúdo outra vez, sem nova cobrança.

## Limites e observações

- `protect_content=True` bloqueia encaminhar e salvar dentro do Telegram, mas não impede print de tela no celular nem foto de outra câmera.
- Pela Bot API comum, o bot só baixa arquivos de até **20 MB** (para o blur). Vídeos maiores ficam com uma capa genérica "VIDEO BLOQUEADO".
- O FSM usa `MemoryStorage`: se o bot reiniciar no meio de um `/novo_pack`, a criadora começa de novo. Para produção com vários processos, troque por `RedisStorage`.
- O Telegram restringe conteúdo adulto explícito em canais públicos, e os gateways PIX costumam exigir cadastro da categoria do negócio. Confira os termos de ambos e garanta que todo conteúdo vendido é de maiores de 18 anos e publicado com consentimento.
