"""Escopa uma camada CSS para um seletor de raiz (ex.: :root[data-estilo="neon"]).
Uso: python3 escopar.py ESCOPO PREFIXO_KEYFRAMES < entrada.css > saida.css"""
import re, sys
escopo, pref = sys.argv[1], sys.argv[2]
css = sys.stdin.read()
css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
kfs = re.findall(r"@keyframes\s+([\w-]+)", css)

def sel1(s):
    s = s.strip()
    if not s: return s
    if s.startswith(":root"): return escopo + s[5:]
    if s.startswith("html"): return escopo + s[4:] if s[4:5] in ("", ":", "[", " ", ".") and s[4:5] != " " else escopo + " " + s[4:].lstrip() if s.startswith("html ") else escopo + s[4:]
    return escopo + " " + s

def split_sel(s):
    out, d, cur = [], 0, ""
    for ch in s:
        if ch == "(": d += 1
        if ch == ")": d -= 1
        if ch == "," and d == 0: out.append(cur); cur = ""
        else: cur += ch
    out.append(cur); return out

def renomear(decl):
    for k in kfs:
        decl = re.sub(rf"(?<![\w-]){re.escape(k)}(?![\w-])", pref + k, decl)
    return decl

def proc(t):
    out, i = [], 0
    while i < len(t):
        j = t.find("{", i)
        if j < 0: out.append(t[i:]); break
        head = t[i:j].strip()
        d, k = 1, j + 1
        while d:
            if t[k] == "{": d += 1
            elif t[k] == "}": d -= 1
            k += 1
        body = t[j+1:k-1]
        if head.startswith("@media") or head.startswith("@supports"):
            out.append(head + "{" + proc(body) + "}")
        elif head.startswith("@keyframes"):
            nome = head.split()[1]
            out.append("@keyframes " + pref + nome + "{" + body + "}")
        elif head.startswith("@font-face"):
            out.append(head + "{" + body + "}")
        else:
            out.append(",".join(sel1(s) for s in split_sel(head)) + "{" + renomear(body) + "}")
        out.append("\n"); i = k
    return "".join(out)
print(proc(css))
