#!/usr/bin/env python3
"""
Robô semanal da Roleta da próxima leitura.

Gera data/pool.json com:
  trending  – livros em alta na semana, com score de trending, nota, nº de avaliações,
              capa e perfil de tags (proporção de shelves do Goodreads)
  catalog   – catálogo de "sementes": livros que a pessoa provavelmente amou
              (trending + mais populares por gênero + títulos que visitantes buscaram e não achamos)

Fontes, por ordem de peso:
  1. Goodreads  "most read this week" por gênero  (base majoritariamente EUA)   peso 1.0
  2. Goodreads  shelves booktok / tiktok-made-me-read-it                         peso 0.9
  3. Open Library trending semanal (API oficial, serve de rede de segurança)     peso 0.5

Se o Goodreads bloquear o robô (403 / captcha), o script cai automaticamente em
modo "openlibrary": tudo vem da API oficial (subjects viram tags). Mais pobre, mas
a roleta continua viva.

Uso:
  python build_pool.py                 # atualização semanal (trending + sementes que faltam)
  python build_pool.py --catalog       # também reconstrói o catálogo de sementes (mensal)
  python build_pool.py --source openlibrary   # força o modo sem Goodreads
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------- config
ROOT = Path(__file__).parent
OUT = ROOT / "data" / "pool.json"
MISSES = ROOT / "data" / "misses.json"      # títulos que visitantes buscaram e não achamos
CACHE = ROOT / "data" / "books_cache.json"  # perfil de cada livro já visitado (evita rescrapear)

GENRES = ["fantasy", "science-fiction", "romance", "thriller", "mystery", "horror", "young-adult"]
BOOKTOK_SHELVES = ["booktok", "tiktok-made-me-read-it", "romantasy"]
SOURCE_WEIGHT = {"gr_most_read": 1.0, "gr_shelf": 0.9, "ol_trending": 0.5}

TRENDING_PER_LIST = 50       # quantos livros pegar de cada lista
TRENDING_MAX = 300           # tamanho máximo do pool de trending
CATALOG_PAGES_PER_GENRE = 4  # páginas de ~50 livros por gênero no modo --catalog (≈ 1.400 livros)
TOP_SHELVES = 40             # shelves por livro
SLEEP = 1.0                  # segundos entre requisições ao Goodreads (seja gentil)
TIME_BUDGET_MIN = 300        # para antes do limite de 6 h do GitHub Actions e grava o que já tem

# shelves que não dizem nada sobre o conteúdo
NOISE = re.compile(
    r"^(to-read|currently-reading|read|owned|own|owned-books|books-i-own|favorites|favourites|"
    r"kindle|ebook|e-book|audiobook|audiobooks|audio|library|dnf|did-not-finish|abandoned|"
    r"default|wish-list|wishlist|to-buy|borrowed|physical|hardcover|paperback|signed|"
    r"series|standalone|reread|re-read|maybe|tbr|netgalley|arc|arcs|buddy-read|"
    r"book-club|bookclub|read-in-\d+|\d{4}|\d{4}-reads?|\d{4}-releases?|\d{4}-tbr|"
    r"fiction|novels?|books|adult|english|my-books|all-time-favorites|5-stars?|4-stars?|"
    r"3-stars?|favorite-books|top-reads)$"
)

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
session = requests.Session()
session.headers.update(UA)


# ----------------------------------------------------------------------------- utils
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def slug(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def get(url: str, **kw) -> requests.Response | None:
    for attempt in range(3):
        try:
            r = session.get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                log(f"  bloqueado ({r.status_code}) em {url}")
                return None
        except requests.RequestException as e:
            log(f"  erro {e} em {url}")
        time.sleep(2 * (attempt + 1))
    return None


def load_json(p: Path, default):
    return json.loads(p.read_text()) if p.exists() else default


# ----------------------------------------------------------------------------- goodreads: listas
def gr_book_ids(html: str) -> list[str]:
    """Ids de livro na ordem em que aparecem na página (sem repetir)."""
    seen, out = set(), []
    for m in re.finditer(r"/book/show/(\d+)", html):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def gr_most_read(genre: str) -> list[str]:
    r = get(f"https://www.goodreads.com/genres/most_read/{genre}")
    time.sleep(SLEEP)
    return gr_book_ids(r.text)[:TRENDING_PER_LIST] if r else []


def gr_shelf(shelf: str, page: int = 1) -> list[str]:
    r = get(f"https://www.goodreads.com/shelf/show/{shelf}", params={"page": page})
    time.sleep(SLEEP)
    return gr_book_ids(r.text)[:TRENDING_PER_LIST] if r else []


# ----------------------------------------------------------------------------- goodreads: um livro
def _walk(obj, want: str):
    """Procura recursivamente dicionários que tenham a chave `want`."""
    if isinstance(obj, dict):
        if want in obj:
            yield obj
        for v in obj.values():
            yield from _walk(v, want)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, want)


def gr_book(book_id: str) -> dict | None:
    """Título, autor, série, nota, capa, gêneros e shelves de um livro do Goodreads."""
    r = get(f"https://www.goodreads.com/book/show/{book_id}")
    time.sleep(SLEEP)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    nd = soup.find("script", id="__NEXT_DATA__")
    if not nd:
        return None
    data = json.loads(nd.string)
    apollo = data.get("props", {}).get("pageProps", {}).get("apolloState", {})

    book = next((v for k, v in apollo.items() if k.startswith("Book:") and v.get("titleComplete")), None)
    work = next((v for k, v in apollo.items() if k.startswith("Work:")), {})
    if not book:
        return None

    author = next((v["name"] for k, v in apollo.items() if k.startswith("Contributor:") and v.get("name")), "")
    series = next((v["title"] for k, v in apollo.items() if k.startswith("Series:") and v.get("title")), "")
    stats = work.get("stats") or book.get("stats") or {}
    details = book.get("details") or {}
    pub = details.get("publicationTime") or (work.get("details") or {}).get("publicationTime")
    year = int(time.gmtime(pub / 1000).tm_year) if pub else None

    genres = [g["genre"]["name"].lower().replace(" ", "-") for g in book.get("bookGenres", []) if g.get("genre")]

    info = {
        "id": book_id,
        "t": book.get("title") or book["titleComplete"],
        "a": author,
        "s": series,
        "y": year,
        "rating": stats.get("averageRating"),
        "n": stats.get("ratingsCount"),
        "cover": book.get("imageUrl"),
        "genres": genres,
        "tags": gr_shelves(book_id),
    }
    # gêneros do Goodreads entram como tags com peso médio, caso as shelves venham vazias
    if not info["tags"]:
        info["tags"] = {g: 1.0 / max(1, len(genres)) for g in genres}
    return info


def gr_shelves(book_id: str) -> dict[str, float]:
    """Proporção de cada shelf de conteúdo (top N) para o livro."""
    r = get(f"https://www.goodreads.com/book/shelves/{book_id}")
    time.sleep(SLEEP)
    if not r:
        return {}
    counts: dict[str, int] = {}
    for m in re.finditer(r'/shelf/show/([\w\-]+)"[^>]*>[^<]*</a>\s*</div>\s*<div class="smallText">\s*([\d,]+)\s+(?:people|person)', r.text):
        name, n = m.group(1).lower(), int(m.group(2).replace(",", ""))
        if not NOISE.match(name):
            counts[name] = counts.get(name, 0) + n
    if not counts:  # layout mudou? tenta um casamento mais frouxo
        for m in re.finditer(r"/shelf/show/([\w\-]+)[^\d]{0,200}?([\d,]{2,})\s+(?:people|person)", r.text):
            name, n = m.group(1).lower(), int(m.group(2).replace(",", ""))
            if not NOISE.match(name):
                counts[name] = counts.get(name, 0) + n
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:TOP_SHELVES]
    tot = sum(n for _, n in top) or 1
    return {k: round(n / tot, 4) for k, n in top}


# ----------------------------------------------------------------------------- open library (fallback)
OL_SUBJECT_MAP = {
    "fantasy": "fantasy", "science fiction": "science-fiction", "romance": "romance",
    "thriller": "thriller", "suspense": "thriller", "mystery": "mystery", "detective": "mystery",
    "horror": "horror", "ghost": "horror", "dragons": "dragons", "magic": "magic", "witches": "witches",
    "vampires": "vampires", "fairies": "fae", "faeries": "fae", "dystopia": "dystopia",
    "space": "space", "time travel": "time-travel", "mythology": "mythology", "gods": "mythology",
    "epic": "epic", "quest": "quest", "war": "war", "political": "political", "humor": "humor",
    "comedy": "humor", "young adult": "ya", "juvenile": "ya", "coming of age": "coming-of-age",
    "love": "romance", "gothic": "gothic", "psychological": "psychological", "historical": "historical",
    "adventure": "adventure", "assassins": "assassins", "heist": "heist", "thieves": "heist",
    "literary": "literary", "retelling": "retelling", "apocalyp": "apocalyptic", "aliens": "first-contact",
    "robots": "ai", "artificial intelligence": "ai", "murder": "mystery", "serial killer": "thriller",
}


def ol_tags(subjects: list[str]) -> dict[str, float]:
    counts: dict[str, float] = defaultdict(float)
    for s in subjects[:60]:
        s = s.lower()
        for key, tag in OL_SUBJECT_MAP.items():
            if key in s:
                counts[tag] += 1
    tot = sum(counts.values()) or 1
    return {k: round(v / tot, 4) for k, v in counts.items()}


def ol_work(key: str) -> dict | None:
    """Monta o perfil de um work do Open Library (key = '/works/OL123W')."""
    r = get("https://openlibrary.org/search.json",
            params={"q": f"key:{key}", "fields": "key,title,author_name,first_publish_year,subject,"
                                                "ratings_average,ratings_count,cover_i"})
    if not r or not r.json().get("docs"):
        return None
    d = r.json()["docs"][0]
    return {
        "id": "ol" + key.rsplit("/", 1)[-1],
        "t": d.get("title", ""),
        "a": (d.get("author_name") or [""])[0],
        "s": "",
        "y": d.get("first_publish_year"),
        "rating": d.get("ratings_average"),
        "n": d.get("ratings_count"),
        "cover": f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else None,
        "genres": [],
        "tags": ol_tags(d.get("subject") or []),
    }


def ol_trending() -> list[str]:
    r = get("https://openlibrary.org/trending/weekly.json", params={"limit": TRENDING_PER_LIST})
    if not r:
        return []
    return [w["key"] for w in r.json().get("works", []) if w.get("key")]


def ol_popular(subject: str, page: int) -> list[str]:
    r = get("https://openlibrary.org/search.json",
            params={"subject": subject, "sort": "rating", "fields": "key", "limit": 100, "page": page})
    return [d["key"] for d in r.json().get("docs", [])] if r else []


# ----------------------------------------------------------------------------- scoring
def bayes(rating, n, prior_mean=3.9, prior_n=2000) -> float | None:
    """Média bayesiana: puxa livros com poucas avaliações para a média geral."""
    if rating is None or not n:
        return None
    return (rating * n + prior_mean * prior_n) / (n + prior_n)


def trending_scores(lists: list[tuple[str, list[str]]]) -> dict[str, float]:
    """Combina posição em cada lista × peso da fonte; devolve score 0–100 por id."""
    raw: dict[str, float] = defaultdict(float)
    for source, ids in lists:
        w = SOURCE_WEIGHT[source]
        for rank, bid in enumerate(ids):
            raw[bid] += w * (1 - rank / max(1, len(ids)))
    if not raw:
        return {}
    top = max(raw.values())
    return {bid: round(100 * v / top, 1) for bid, v in raw.items()}


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", action="store_true", help="reconstrói o catálogo de sementes (mensal)")
    ap.add_argument("--source", choices=["goodreads", "openlibrary"], default="goodreads")
    ap.add_argument("--limit", type=int, default=0, help="só para testar: processa no máximo N livros")
    ap.add_argument("--minutes", type=int, default=TIME_BUDGET_MIN, help="orçamento de tempo em minutos")
    args = ap.parse_args()
    t0 = time.time()
    log(f"início: catálogo={'sim' if args.catalog else 'não'}  fonte={args.source}  orçamento={args.minutes} min")

    def out_of_time() -> bool:
        return (time.time() - t0) / 60 > args.minutes

    cache: dict[str, dict] = load_json(CACHE, {})
    prev = load_json(OUT, {"trending": [], "catalog": []})
    misses: list[str] = load_json(MISSES, [])
    mode = args.source

    # -- 1. listas de trending
    lists: list[tuple[str, list[str]]] = []
    if mode == "goodreads":
        probe = get("https://www.goodreads.com/genres/most_read/fantasy")
        if probe is None:
            log("Goodreads indisponível para o robô — caindo para o modo openlibrary")
            mode = "openlibrary"
        else:
            lists.append(("gr_most_read", gr_book_ids(probe.text)[:TRENDING_PER_LIST]))
            for g in GENRES[1:]:
                lists.append(("gr_most_read", gr_most_read(g)))
            for s in BOOKTOK_SHELVES:
                lists.append(("gr_shelf", gr_shelf(s)))
    ol_ids = ol_trending()
    lists.append(("ol_trending", ol_ids))

    scores = trending_scores(lists)
    ranked = sorted(scores, key=lambda k: -scores[k])[:TRENDING_MAX]
    if args.limit:
        ranked = ranked[:args.limit]
    log(f"modo={mode}  listas={len(lists)}  trending={len(ranked)}")

    # -- 2. perfil de cada livro (com cache)
    def profile(bid: str) -> dict | None:
        if bid in cache and cache[bid].get("tags"):
            return cache[bid]
        if bid.startswith("/works/"):
            info = ol_work(bid)
        elif mode == "goodreads" and bid.isdigit():
            info = gr_book(bid)
        else:
            return None  # id de outra fonte (ou do pool de exemplo): ignora
        if info:
            cache[bid] = info
            CACHE.write_text(json.dumps(cache, ensure_ascii=False))
        return info

    trending = []
    for i, bid in enumerate(ranked, 1):
        if out_of_time():
            log("  orçamento de tempo esgotado no trending; gravando o que já tem")
            break
        info = profile(bid)
        if info and info["tags"]:
            trending.append({**info, "tr": scores[bid], "score": bayes(info["rating"], info["n"])})
        if i % 20 == 0:
            log(f"  {i}/{len(ranked)} perfis")

    # -- 3. catálogo de sementes
    catalog_ids: set[str] = {b["id"] for b in prev.get("catalog", [])}
    catalog_ids.update(load_json(ROOT / "data" / "pending.json", []))
    if args.catalog or not catalog_ids:
        log("reconstruindo catálogo de sementes…")
        if mode == "goodreads":
            for g in GENRES:
                for p in range(1, CATALOG_PAGES_PER_GENRE + 1):
                    catalog_ids.update(gr_shelf(g, p))
        else:
            for g in ["fantasy", "science fiction", "romance", "thriller", "mystery"]:
                for p in range(1, 4):
                    catalog_ids.update(ol_popular(g, p))
    # títulos que visitantes buscaram e não achamos entram na próxima rodada
    for title in misses:
        bid = find_id(title, mode)
        if bid:
            catalog_ids.add(bid)
    catalog_ids.update(b["id"] for b in trending)

    # o que já está em cache vai primeiro (de graça); o resto até acabar o tempo
    ordered = sorted(catalog_ids, key=lambda b: (b not in cache, b))
    catalog, pending = [], 0
    for i, bid in enumerate(ordered, 1):
        if out_of_time():
            pending = len(ordered) - i + 1
            log(f"  orçamento de tempo esgotado no catálogo; {pending} sementes ficam para a próxima rodada")
            break
        info = profile(bid)
        if info and info["tags"]:
            catalog.append({**info, "tr": scores.get(bid, 0)})
        if i % 50 == 0:
            log(f"  {i}/{len(catalog_ids)} sementes")

    # -- 4. grava
    out = {
        "updated": date.today().isoformat(),
        "source": mode,
        "trending": sorted(trending, key=lambda b: -b["tr"]),
        "catalog": sorted(catalog, key=lambda b: -(b["n"] or 0)),
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    MISSES.write_text("[]")
    # sementes que não deu tempo de ler ficam anotadas para a próxima rodada completar
    PENDING = ROOT / "data" / "pending.json"
    PENDING.write_text(json.dumps([b for b in ordered if b not in cache]) if pending else "[]")
    log(f"ok: {len(trending)} trending, {len(catalog)} sementes, {pending} pendentes → {OUT}  ({(time.time()-t0)/60:.0f} min)")


def find_id(title: str, mode: str) -> str | None:
    """Acha o id de um título pelo nome (para os 'misses')."""
    if mode == "goodreads":
        r = get("https://www.goodreads.com/search", params={"q": title})
        time.sleep(SLEEP)
        ids = gr_book_ids(r.text) if r else []
        return ids[0] if ids else None
    r = get("https://openlibrary.org/search.json", params={"title": title, "fields": "key", "limit": 1})
    docs = r.json().get("docs", []) if r else []
    return docs[0]["key"] if docs else None


if __name__ == "__main__":
    main()
