import asyncio, json, os, time, datetime as dt
import xml.etree.ElementTree as ET
from urllib.parse import quote
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

AF = "https://v3.football.api-sports.io"
KEY = os.getenv("API_FOOTBALL_KEY", "")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
app = FastAPI(title="Dossiê de Futebol")
_cache: dict = {}

def season_now() -> int:
    t = dt.date.today()
    return t.year if t.month >= 7 else t.year - 1

async def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = await fn()
    _cache[key] = (time.time(), val)
    return val

async def af(path, **params):
    if not KEY:
        raise HTTPException(500, "API_FOOTBALL_KEY não configurada")
    async def go():
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{AF}{path}", params=params, headers={"x-apisports-key": KEY})
            r.raise_for_status()
            return r.json().get("response", [])
    ttl = 86400 if path in ("/players/profiles", "/players/trophies") else 900
    return await cached((path, tuple(sorted(params.items()))), ttl, go)

async def claude(prompt: str, max_tokens=1200) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return ""
    from anthropic import AsyncAnthropic
    msg = await AsyncAnthropic(api_key=key).messages.create(
        model=MODEL, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text

async def fix_name(q: str) -> str:
    out = await claude(f"Corrija o nome do jogador ou time de futebol. Responda só com o nome correto: {q}", 30)
    return out.strip() or q

@app.get("/api/search")
async def search(q: str):
    q = q.strip()
    if len(q) < 3:
        return []
    res = await af("/players/profiles", search=q)
    if not res:
        fixed = await fix_name(q)
        if fixed.lower() != q.lower():
            res = await af("/players/profiles", search=fixed)
    return [{"id": r["player"]["id"], "name": r["player"]["name"],
             "nat": r["player"].get("nationality"), "pos": r["player"].get("position"),
             "photo": r["player"].get("photo")} for r in res[:8]]

async def news(name: str):
    async def go():
        url = ("https://news.google.com/rss/search?q=" + quote(f'"{name}"') + "&hl=pt-BR&gl=BR&ceid=BR:pt-419")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
        items = []
        for it in ET.fromstring(r.text).iterfind(".//item"):
            items.append({"title": it.findtext("title"), "link": it.findtext("link"),
                          "date": it.findtext("pubDate"), "source": it.findtext("source")})
            if len(items) == 12:
                break
        if not items:
            return []
        prompt = ("Classifique cada manchete sobre um jogador de futebol. Categorias: "
                  "transferencia (rumor ou negociação), lesao, polemica (briga, conflito, "
                  "indisciplina, crise no clube), outro. Responda SOMENTE um JSON: lista de "
                  'objetos {"i": indice, "cat": categoria}.\n'
                  + "\n".join(f"{i}: {x['title']}" for i, x in enumerate(items)))
        try:
            tags = json.loads((await claude(prompt, 600)).strip().strip("`").removeprefix("json"))
            for t in tags:
                items[t["i"]]["cat"] = t["cat"]
        except Exception:
            pass
        return items
    return await cached(("news", name), 300, go)

@app.get("/api/player/{pid}")
async def player(pid: int):
    s = season_now()
    prof, trophies, transfers, inj = await asyncio.gather(
        af("/players/profiles", player=pid), af("/players/trophies", player=pid),
        af("/transfers", player=pid), af("/injuries", player=pid, season=s))
    if not prof:
        raise HTTPException(404, "Jogador não encontrado")
    p = prof[0]["player"]
    stats = []
    for yr in range(s, s - 4, -1):
        for row in await af("/players", id=pid, season=yr):
            for st in row["statistics"]:
                stats.append({"season": yr, "team": st["team"]["name"], "league": st["league"]["name"],
                    "games": st["games"]["appearences"], "minutes": st["games"]["minutes"],
                    "goals": st["goals"]["total"], "assists": st["goals"]["assists"],
                    "shots": st["shots"]["total"], "passes": st["passes"]["total"],
                    "tackles": st["tackles"]["total"], "interceptions": st["tackles"]["interceptions"],
                    "yellow": st["cards"]["yellow"], "red": st["cards"]["red"],
                    "fouls_committed": st["fouls"]["committed"], "fouls_drawn": st["fouls"]["drawn"],
                    "saves": st["goals"]["saves"], "rating": st["games"]["rating"]})
    tr = [{"date": t["date"], "from": t["teams"]["out"]["name"], "to": t["teams"]["in"]["name"],
           "type": t["type"]} for x in transfers for t in x["transfers"]]
    injuries = [{"date": i["fixture"]["date"][:10], "type": i["player"]["type"],
                 "reason": i["player"]["reason"], "team": i["team"]["name"]} for i in inj]
    injuries.sort(key=lambda x: x["date"], reverse=True)
    return {"profile": p, "stats": stats, "transfers": sorted(tr, key=lambda x: x["date"] or "", reverse=True),
            "trophies": [{"league": t["league"], "country": t["country"], "season": t["season"],
                          "place": t["place"]} for t in trophies],
            "injuries": injuries[:10], "news": await news(p["name"])}

@app.get("/api/summary/{pid}")
async def summary(pid: int):
    d = await player(pid)
    txt = await claude("Escreva em português um resumo de carreira (até 150 palavras) e destaque "
        "melhor fase e queda de desempenho. Use SOMENTE estes dados:\n"
        + json.dumps({k: d[k] for k in ("profile", "stats", "transfers", "trophies")}, ensure_ascii=False)[:12000], 700)
    return {"summary": txt}

app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/")
def home():
    return FileResponse("static/index.html")
