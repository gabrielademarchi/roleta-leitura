#!/usr/bin/env python3
"""
Newsletter semanal da Roleta da próxima leitura, enviada pelo Kit.

Lê data/pool.json (semana atual) e data/pool_prev.json (semana anterior), monta o e-mail
e cria um broadcast no Kit agendado para daqui a alguns minutos.

  python newsletter.py             # monta e envia (precisa de KIT_API_KEY no ambiente)
  python newsletter.py --preview   # só grava data/newsletter-preview.html, não envia

Conteúdo, nesta ordem:
  1. Nota da semana — o texto de data/nota-da-semana.md, se houver (parágrafos, **negrito**, [links](url))
  2. Os 5 que mais subiram no BookTok esta semana (ou os 5 mais em alta, se não houver semana anterior)
  3. Um por clima — 3 livros para quem quer "se apaixonar", "ir para outro mundo" e "sentir medo/tensão"
  4. Link para a roleta
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------- config
SITE_URL = "https://gabrielademarchi.github.io/roleta-leitura/"
ASSETS = SITE_URL + "assets/"           # logo.png, flag-br.png, flag-us.png hospedados no GitHub Pages
FROM_NAME = "Livros e etc"
KIT_API_KEY = os.environ.get("KIT_API_KEY", "")
SEND_IN_MINUTES = 10                     # margem para cancelar no painel do Kit se algo sair errado

ROOT = Path(__file__).parent
POOL = ROOT / "data" / "pool.json"
PREV = ROOT / "data" / "pool_prev.json"
NOTE = ROOT / "data" / "nota-da-semana.md"
PREVIEW = ROOT / "data" / "newsletter-preview.html"

# paleta do canal
BEGE, AZUL, LARANJA, TINTA, TINTA2, LINHA = "#f9f5ef", "#16289B", "#FC9B4E", "#121B5C", "#3A4380", "#e3dacb"

MOODS = [
    ("Para quem quer se apaixonar", {"romance", "romantasy", "slow-burn", "enemies-to-lovers", "fae"}),
    ("Para quem quer ir para outro mundo", {"fantasy", "epic", "worldbuilding", "science-fiction", "space"}),
    ("Para quem quer sentir medo ou tensão", {"thriller", "horror", "dark", "mystery", "psychological"}),
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def load(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


# ----------------------------------------------------------------------------- seleção
def risers(cur: list[dict], prev: list[dict], n=5) -> tuple[list[dict], bool]:
    """Os n que mais subiram de score; se não há semana anterior, os n mais em alta."""
    def one_per_series(items):
        seen, out = set(), []
        for b in items:
            key = b.get("s") or b["id"]
            if key not in seen:
                seen.add(key)
                out.append(b)
        return out

    if not prev:
        return one_per_series(cur)[:n], False
    before = {b["id"]: b["tr"] for b in prev}
    scored = []
    for b in cur:
        delta = b["tr"] - before.get(b["id"], 0)
        scored.append((delta, b["id"] not in before, b))
    scored.sort(key=lambda x: -x[0])
    picked = one_per_series([dict(b, delta=d, novo=new) for d, new, b in scored])[:n]
    return picked, True


def by_mood(cur: list[dict], exclude: set[str]) -> list[tuple[str, dict]]:
    out = []
    used = set(exclude)
    used_series = {b.get("s") for b in cur if b["id"] in exclude and b.get("s")}
    for label, tags in MOODS:
        best, best_s = None, 0
        for b in cur:
            if b["id"] in used or (b.get("s") and b["s"] in used_series):
                continue
            s = sum(w for k, w in b["tags"].items() if k in tags) * (0.5 + b["tr"] / 100)
            if s > best_s:
                best, best_s = b, s
        if best:
            used.add(best["id"])
            if best.get("s"):
                used_series.add(best["s"])
            out.append((label, best))
    return out


# ----------------------------------------------------------------------------- render
def md_lite(text: str) -> str:
    """Markdown mínimo: parágrafos, **negrito**, *itálico*, [texto](url)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    out = []
    for p in paras:
        p = html.escape(p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"<em>\1</em>", p)
        p = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", rf'<a href="\2" style="color:{AZUL}">\1</a>', p)
        out.append(f'<p style="margin:0 0 14px;font-size:16px;line-height:1.55;color:{TINTA}">{p}</p>')
    return "".join(out)


def flag(b: dict) -> str:
    if b.get("br"):
        return f'<img src="{ASSETS}flag-br.png" width="16" height="16" alt="em português" style="vertical-align:-3px;margin-left:6px">'
    if b.get("br") is False:
        return f'<img src="{ASSETS}flag-us.png" width="16" height="16" alt="só em inglês" style="vertical-align:-3px;margin-left:6px">'
    return ""


def pt_line(b: dict) -> str:
    if b.get("br") and (b.get("t_pt") or b.get("pub_pt")):
        t = f"<em>{html.escape(b['t_pt'])}</em>" if b.get("t_pt") else ""
        pub = f" ({html.escape(b['pub_pt'])})" if b.get("pub_pt") else ""
        return f'<div style="font-size:13px;color:{TINTA2};margin-top:2px">Em português: {t}{pub}</div>'
    return ""


def book_row(b: dict, badge: str = "") -> str:
    cover = (f'<img src="{html.escape(b["cover"])}" width="56" alt="" style="display:block;width:56px;border-radius:3px">'
             if b.get("cover") else
             f'<div style="width:56px;height:84px;background:#fff;border:1px solid {LINHA};border-radius:3px"></div>')
    gr = "https://www.goodreads.com/book/show/" + html.escape(str(b["id"]))
    badge_html = f'<span style="font-size:12px;color:{LARANJA};font-weight:600">{badge}</span><br>' if badge else ""
    return f"""
<tr>
  <td width="56" valign="top" style="padding:10px 14px 10px 0">{cover}</td>
  <td valign="top" style="padding:10px 0;border-bottom:1px solid {LINHA}">
    {badge_html}
    <a href="{gr}" style="font-family:Georgia,'Times New Roman',serif;font-size:18px;color:{TINTA};text-decoration:none;font-weight:600">{html.escape(b["t"])}</a>{flag(b)}
    <div style="font-size:14px;color:{TINTA2}">{html.escape(b.get("a", ""))}{(" · " + html.escape(b["s"])) if b.get("s") else ""}</div>
    {pt_line(b)}
  </td>
</tr>"""


def render(pool: dict, prev: dict, note: str) -> tuple[str, str]:
    cur = pool["trending"]
    top, has_prev = risers(cur, prev.get("trending", []))
    moods = by_mood(cur, {b["id"] for b in top})
    when = datetime.fromisoformat(pool["updated"]).strftime("%d/%m")

    subject = f"Em alta esta semana: {top[0]['t']} e mais {len(top) - 1}" if top else "Os livros em alta esta semana"
    h = f"<h2 style=\"font-family:Georgia,'Times New Roman',serif;font-weight:600;font-size:22px;margin:28px 0 6px;color:{TINTA}\">"

    rows_top = ""
    for b in top:
        badge = ("Novo na lista" if b.get("novo") else (f"Subiu {b['delta']:.0f} pontos" if b.get("delta", 0) > 0 else "")) if has_prev else ""
        rows_top += book_row(b, badge)
    rows_mood = "".join(book_row(b, label) for label, b in moods)

    body = f"""<!DOCTYPE html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{html.escape(subject)}</title></head>
<body style="margin:0;padding:0;background:{BEGE}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BEGE}">
<tr><td align="center" style="padding:28px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;font-family:Helvetica,Arial,sans-serif;color:{TINTA}">
  <tr><td style="padding-bottom:18px"><img src="{ASSETS}logo.png" width="64" alt="{FROM_NAME}" style="display:block"></td></tr>
  <tr><td>
    <h1 style="font-family:Georgia,'Times New Roman',serif;font-weight:400;font-size:28px;line-height:1.15;margin:0 0 6px">O que o mundo está lendo esta semana</h1>
    <p style="margin:0 0 10px;font-size:14px;color:{TINTA2}">Semana de {when} · os títulos aparecem em inglês, como no Goodreads; a bandeirinha mostra quando já existe edição em português.</p>
  </td></tr>
  {f'<tr><td style="padding-top:18px">{md_lite(note)}</td></tr>' if note.strip() else ''}
  <tr><td>{h}{"Os 5 que mais subiram" if has_prev else "Os 5 mais em alta"}</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows_top}</table></td></tr>
  <tr><td>{h}Um para cada clima</h2>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows_mood}</table></td></tr>
  <tr><td style="padding:30px 0 10px">
    <a href="{SITE_URL}" style="display:inline-block;background:{AZUL};color:{BEGE};text-decoration:none;font-weight:600;font-size:16px;padding:14px 24px;border-radius:999px">Girar a roleta da próxima leitura</a>
  </td></tr>
  <tr><td style="padding-top:18px;font-size:13px;line-height:1.5;color:{TINTA2}">
    Você recebe este e-mail porque se inscreveu na roleta do {FROM_NAME}. Os dados vêm do Goodreads e do BookTok e são atualizados toda segunda-feira.
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
    return subject, body


# ----------------------------------------------------------------------------- envio
def send_kit(subject: str, body: str) -> None:
    send_at = (datetime.now(timezone.utc) + timedelta(minutes=SEND_IN_MINUTES)).replace(microsecond=0).isoformat()
    r = requests.post(
        "https://api.kit.com/v4/broadcasts",
        headers={"X-Kit-Api-Key": KIT_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
        json={"subject": subject, "content": body, "description": f"Roleta · {subject}", "public": False, "send_at": send_at},
        timeout=60,
    )
    if r.status_code not in (200, 201):
        log(f"Kit respondeu {r.status_code}: {r.text[:500]}")
        sys.exit(1)
    bid = r.json().get("broadcast", {}).get("id")
    log(f"broadcast {bid} agendado para {send_at} (UTC): {subject}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="só gera data/newsletter-preview.html")
    ap.add_argument("--force", action="store_true", help="envia mesmo se o pool não foi atualizado hoje")
    args = ap.parse_args()

    pool = load(POOL, None)
    if not pool or not pool.get("trending"):
        log("sem pool.json; nada a enviar")
        return
    prev = load(PREV, {})
    note = NOTE.read_text(encoding="utf-8") if NOTE.exists() else ""

    subject, body = render(pool, prev, note)
    PREVIEW.write_text(body, encoding="utf-8")
    log(f"prévia gravada em {PREVIEW}  ·  assunto: {subject}")

    if args.preview:
        return
    if not KIT_API_KEY:
        log("KIT_API_KEY não configurada; só a prévia foi gerada")
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if pool.get("updated") != today and not args.force:
        log(f"pool.json é de {pool.get('updated')}, não de hoje — não envio (use --force para mandar mesmo assim)")
        return
    send_kit(subject, body)


if __name__ == "__main__":
    main()
