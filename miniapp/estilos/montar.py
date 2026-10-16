"""Gera o bloco de visuais alternativos dentro do index.html.

Cada visual é um CSS comum, escrito como se fosse a única folha de estilo; este script prefixa tudo com o
seletor do visual (:root[data-estilo="..."]) e cola o resultado entre os marcadores do index.html.
O "camarim" (vermelho, padrão) é o CSS que já está no index.html e não passa por aqui.
Rodar depois de mexer em qualquer arquivo desta pasta:  python3 miniapp/estilos/montar.py
"""
import pathlib, subprocess, sys

AQUI = pathlib.Path(__file__).parent
INDEX = AQUI.parent / "index.html"
ALT = ["backstage", "neon", "album", "revista", "flip"]
TODOS = ":root[data-alt]"           # o script marca data-alt em qualquer visual que não seja o camarim
INICIO, FIM = "/* ═══ VISUAIS ALTERNATIVOS: gerado por estilos/montar.py, não editar aqui ═══ */", "/* ═══ fim dos visuais alternativos ═══ */"


def escopar(arquivo: str, escopo: str, prefixo: str) -> str:
    return subprocess.run([sys.executable, str(AQUI / "escopar.py"), escopo, prefixo], input=(AQUI / arquivo).read_text(),
                          capture_output=True, text=True, check=True).stdout


partes = [(AQUI / "fontes.css").read_text(),
          # base comum (estrutura de cartaz, ingressos, dock, comprovante); é também o visual Backstage
          escopar("backstage-base.css", TODOS, "bs-")]
for e in ALT[1:]:
    # [data-alt] a mais: o visual ganha da base comum (inclusive do bloco do modo claro dela)
    partes.append(escopar(f"{e}.css", f':root[data-alt][data-estilo="{e}"]', e[:2] + "-"))
bloco = INICIO + "\n" + "\n".join(partes) + FIM
html = INDEX.read_text()
if INICIO in html:
    a, b = html.index(INICIO), html.index(FIM) + len(FIM)
    html = html[:a] + bloco + html[b:]
else:
    html = html.replace("</style>", bloco + "\n</style>", 1)
INDEX.write_text(html)
print(f"ok: {len(bloco)//1024} KB de CSS para {len(ALT)} visuais")
