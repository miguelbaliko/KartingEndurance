"""
Cloud recorder for Apex Timing, Kartodromo de Alcanede (live.apex-timing.com/kartalcanede).

Keeps a connection to the live timing feed and stores everything in Postgres:
  raw       every server message (so it can be re-parsed later)
  laps      one row per completed lap: kart, team, lap time, S1/S2/S3, position, gap
  pits      pit in / pit out events
  events    session titles, flags, race control messages (penalties)

HTTP:  /         status
       /report   good times by sector, kart and hour
       /laps.csv /pits.csv /events.csv
"""
import asyncio, csv, html, io, json, os, re, ssl, statistics, time
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp
import asyncpg
from aiohttp import web

HOST = os.environ.get("APEX_HOST", "live-data.apex-timing.com")
BASE = int(os.environ.get("APEX_PORT", "9740"))   # kartalcanede/javascript/config.js
EVENT = os.environ.get("APEX_EVENT", "kartalcanede")
PAGE = f"https://live.apex-timing.com/{EVENT}/"
FUNCS = f"https://{HOST}/live-timing/commonv2/functions/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"
# Same transports the Apex page uses (see warroom/app.py _find_apex_endpoints):
# WebSocket on configPort+3, AJAX polling fallback on configPort+4.
WS_URL = f"wss://{HOST}:{BASE + 3}/"
DB_URL = os.environ["DATABASE_URL"]
SELF_URL = os.environ.get("RENDER_EXTERNAL_URL")

STATE = {"connected": None, "url": None, "last_msg": None, "msgs": 0, "started": None, "tries": []}

SCHEMA = """
create table if not exists raw (id bigserial primary key, ts timestamptz default now(), msg text);
create table if not exists laps (id bigserial primary key, ts timestamptz default now(), session text,
  row_id text, kart text, team text, lap_s double precision, s1 text, s2 text, s3 text,
  pos text, gap text, laps text, on_track text, flag text, cells jsonb);
create table if not exists pits (id bigserial primary key, ts timestamptz default now(), session text,
  row_id text, kart text, team text, event text);
create table if not exists events (id bigserial primary key, ts timestamptz default now(), kind text, text text);
create table if not exists archive (sid text primary key, name text, body text, fetched timestamptz default now());
create table if not exists archive_rows (sid text, name text, row_id text, kart text, team text,
  best_lap double precision, cells jsonb);
"""


def secs(v):
    if not v:
        return None
    m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", v.strip().replace(",", "."))
    return int(m.group(1) or 0) * 60 + float(m.group(2)) if m else None


def text(h):
    return html.unescape(re.sub(r"<[^>]+>", " ", h or "")).strip()


class Feed:
    def __init__(self, pool):
        self.pool = pool
        self.cols, self.rows = {}, defaultdict(dict)
        self.session, self.flag = "", ""

    async def ex(self, sql, *a):
        async with self.pool.acquire() as c:
            await c.execute(sql, *a)

    def grid(self, g):
        self.cols.clear(); self.rows.clear()
        for m in re.finditer(r'<td([^>]*)>', g):
            attrs = m.group(1)
            cid = re.search(r'data-id="(c\d+)"', attrs)
            typ = re.search(r'data-type="([^"]*)"', attrs)
            if cid and typ:
                self.cols[cid.group(1)] = typ.group(1)
        # Cells sometimes carry data-id on an inner <p>/<div> (rank, kart number), so match any tag.
        for m in re.finditer(r'<(td|p|div)[^>]*data-id="(r\d+)(c\d+)"[^>]*>(.*?)</\1>', g, re.S):
            _, r, c, v = m.groups()
            self.rows[r][self.cols.get(c, c)] = text(v)
        for m in []:
            r, c, v = m.groups()
            self.rows[r][self.cols.get(c, c)] = text(v)

    async def handle(self, line):
        parts = line.split("|", 2)
        if len(parts) < 3:
            return
        key, kind, val = parts
        if key == "grid":
            self.grid(val)
            await self.ex("insert into events(kind,text) values($1,$2)", "grid", f"{len(self.rows)} rows, cols {self.cols}")
        elif key in ("title1", "title2"):
            if val:
                self.session = text(val)
                await self.ex("insert into events(kind,text) values($1,$2)", key, self.session)
        elif key == "light":
            self.flag = kind
            await self.ex("insert into events(kind,text) values($1,$2)", "flag", kind)
        elif key in ("com", "msg"):
            if val:
                await self.ex("insert into events(kind,text) values($1,$2)", key, text(val))
        else:
            m = re.fullmatch(r"(r\d+)(c\d+)?", key)
            if not m:
                return
            r, c = m.groups()
            st = self.rows[r]
            if c:
                t = self.cols.get(c, c)
                st[t] = text(val)
                if t == "llp":
                    lap = secs(st[t])
                    if lap and lap > 0:
                        await self.ex(
                            "insert into laps(session,row_id,kart,team,lap_s,s1,s2,s3,pos,gap,laps,on_track,flag,cells)"
                            " values($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)",
                            self.session, r, st.get("no", ""), st.get("dr", ""), lap,
                            st.get("s1", ""), st.get("s2", ""), st.get("s3", ""), st.get("rk", ""),
                            st.get("gap", ""), st.get("tlp", ""), st.get("otr", ""), self.flag, json.dumps(st))
            elif kind in ("*in", "*out"):
                await self.ex("insert into pits(session,row_id,kart,team,event) values($1,$2,$3,$4,$5)",
                              self.session, r, st.get("no", ""), st.get("dr", ""), kind[1:])


async def store_frame(pool, feed, frame):
    STATE["last_msg"] = datetime.now(timezone.utc).isoformat()
    STATE["msgs"] += 1
    async with pool.acquire() as c:
        await c.execute("insert into raw(msg) values($1)", frame)
    for line in frame.replace("\r", "").split("\n"):
        if line:
            try:
                await feed.handle(line)
            except Exception as e:
                print("parse error", e, line[:100], flush=True)


def note(msg):
    STATE["tries"] = (STATE["tries"] + [f"{datetime.now(timezone.utc):%d %H:%M} {msg}"])[-15:]
    print(msg, flush=True)


async def recorder(app):
    import websockets
    pool = app["pool"]
    feed = Feed(pool)
    ctx = ssl.create_default_context()
    hdrs = {"User-Agent": UA, "Referer": PAGE}
    while True:
        got = 0
        try:
            async with websockets.connect(WS_URL, ssl=ctx, open_timeout=15, ping_interval=20, max_size=None,
                                          origin="https://live.apex-timing.com",
                                          extra_headers=[("User-Agent", UA)]) as ws:
                STATE.update(connected=datetime.now(timezone.utc).isoformat(), url=WS_URL)
                note("ws connected")
                async for frame in ws:
                    if isinstance(frame, bytes):
                        frame = frame.decode("utf-8", "replace")
                    got += 1
                    await store_frame(pool, feed, frame)
            note(f"ws closed after {got} frames")
        except Exception as e:
            note(f"ws failed {type(e).__name__} {e}"[:200])
        STATE["connected"] = None
        if got:
            await asyncio.sleep(3)
            continue
        # WebSocket gave nothing: poll the AJAX fallback for ~10 minutes, then try the socket again.
        init, index, counter = "1", "0", 0
        STATE["url"] = "ajax"
        async with aiohttp.ClientSession(headers=hdrs) as s:
            for _ in range(120):
                counter += 1
                q = {"version": "1.0.0", "init": init, "index": index, "port": BASE + 4,
                     "counter": counter, "duration": 0, "id": 0, "ignored": ""}
                try:
                    async with s.get(FUNCS + "live_ajax.php", params=q, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        body = await r.text()
                    parts = body.split("@", 2)
                    if len(parts) == 3:
                        init, index = parts[0], parts[1]
                        if parts[2] == "REFRESH_BROWSER":
                            init, index = "1", "0"
                        elif parts[2]:
                            await store_frame(pool, feed, parts[2])
                    elif counter % 60 == 1:
                        note(f"ajax odd reply {body[:80]!r}")
                except Exception as e:
                    if counter % 60 == 1:
                        note(f"ajax failed {type(e).__name__} {e}"[:200])
                await asyncio.sleep(5)


async def archiver(app):
    """Pull every archived Apex session for this track (classification, best laps) every 3 h."""
    pool = app["pool"]
    hdrs = {"User-Agent": UA, "Referer": PAGE, "Content-Type": "application/x-www-form-urlencoded"}
    async with aiohttp.ClientSession(headers=hdrs) as s:
        async def req(r):
            async with s.post(FUNCS + "request.php", data={"port": BASE, "request": r},
                              timeout=aiohttp.ClientTimeout(total=30)) as resp:
                return (await resp.text()).strip()
        while True:
            try:
                body = await req("S#")
                sessions = [l.strip().partition("#") for l in body.split("\n") if l.strip()] if body and body != "error" else []
                note(f"archive: {len(sessions)} sessions listed")
                async with pool.acquire() as c:
                    have = {r["sid"] for r in await c.fetch("select sid from archive")}
                    for r in await c.fetch("select sid, name, kart, team, best_lap, cells->>'tlp' laps from archive_rows "
                                           "order by sid, best_lap nulls last"):
                        print(f"ARCHIVE {r['sid']} | {r['name']} | best {r['best_lap']} | no {r['kart']} | {r['team']} | laps {r['laps']}", flush=True)
                for sid, _, name in sessions:
                    if not sid or sid in have:
                        continue
                    res = await req(f"S#{sid}")
                    payload = res.split("@", 2)[2] if res.count("@") >= 2 else res
                    f = Feed(pool)
                    for line in payload.replace("\r", "").split("\n"):
                        if line.startswith("grid|"):
                            f.grid(line.split("|", 2)[2])
                    async with pool.acquire() as c:
                        await c.execute("insert into archive(sid,name,body) values($1,$2,$3) on conflict do nothing", sid, name, res)
                        for r, st in f.rows.items():
                            best = secs(st.get("blp", "")) or secs(st.get("llp", ""))
                            await c.execute("insert into archive_rows(sid,name,row_id,kart,team,best_lap,cells) values($1,$2,$3,$4,$5,$6,$7)",
                                            sid, name, r, st.get("no", ""), st.get("dr", ""), best, json.dumps(st))
                    top = sorted(((st.get("blp") or st.get("llp") or "", st.get("no", ""), st.get("dr", ""), st.get("tlp", ""))
                                  for st in f.rows.values()), key=lambda x: secs(x[0]) or 999)
                    print(f"ARCHIVE {sid} | {name} | {len(f.rows)} rows | cols {sorted(set(f.cols.values()))}", flush=True)
                    for b, k, t, n in top[:40]:
                        print(f"ARCHIVE {sid} | best {b} | no {k} | {t} | laps {n}", flush=True)
                    await asyncio.sleep(1)
            except Exception as e:
                note(f"archive failed {type(e).__name__} {e}"[:200])
            await asyncio.sleep(3 * 3600)


async def summary(app):
    """Every 15 min, log a compact summary so it can be read from the Render logs."""
    pool = app["pool"]
    while True:
        await asyncio.sleep(900)
        try:
            async with pool.acquire() as c:
                n = {t: await c.fetchval(f"select count(*) from {t}") for t in ("raw", "laps", "pits", "events")}
                last = await c.fetchval("select max(ts) from raw")
                sess = await c.fetch("select session, count(*) n, min(lap_s) best, percentile_cont(0.25) within group (order by lap_s) p25, "
                                     "percentile_cont(0.5) within group (order by lap_s) med from laps "
                                     "where ts > now() - interval '15 minutes' group by session")
                sec = await c.fetchrow("select min(nullif(replace(s1,',','.'),'')::float) s1, min(nullif(replace(s2,',','.'),'')::float) s2, "
                                       "min(nullif(replace(s3,',','.'),'')::float) s3 from laps where s1 ~ '^[0-9.,]+$' and s2 ~ '^[0-9.,]+$' and s3 ~ '^[0-9.,]+$'")
                ev = await c.fetch("select ts, kind, left(text,120) t from events where ts > now() - interval '15 minutes' order by id")
            print(f"SUMMARY counts {n} last_frame {last} ws {STATE['connected'] and 'up' or 'down'} msgs {STATE['msgs']}", flush=True)
            for r in sess:
                print(f"SUMMARY 15min {r['session']} | laps {r['n']} | best {r['best']:.3f} | p25 {r['p25']:.3f} | median {r['med']:.3f}", flush=True)
            if sec and sec["s1"]:
                print(f"SUMMARY sector bests S1 {sec['s1']} S2 {sec['s2']} S3 {sec['s3']}", flush=True)
            async with pool.acquire() as c:
                for r in await c.fetch("select ts, kart, team, lap_s, s1, s2, s3 from laps order by id desc limit 5"):
                    print(f"SUMMARY lap {r['ts']:%H:%M:%S} kart {r['kart']} {r['team']} {r['lap_s']} | {r['s1']} {r['s2']} {r['s3']}", flush=True)
                for r in await c.fetch("select left(msg,300) m from raw order by id desc limit 2"):
                    print(f"SUMMARY raw {r['m']!r}", flush=True)
            for e in ev:
                print(f"SUMMARY event {e['ts']:%H:%M} {e['kind']} {e['t']}", flush=True)
        except Exception as e:
            print("summary failed", e, flush=True)


async def weather(app):
    """Every hour, log the hourly forecast for the track (Open-Meteo) so the race page can be refreshed from the logs."""
    url = ("https://api.open-meteo.com/v1/forecast?latitude=39.415&longitude=-8.83&timezone=Europe/Lisbon&forecast_days=3"
           "&hourly=temperature_2m,apparent_temperature,precipitation,precipitation_probability,wind_speed_10m,wind_direction_10m,cloud_cover")
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    body = await r.json(content_type=None)
                if "hourly" not in body:
                    raise RuntimeError(f"{r.status} {str(body)[:200]}")
                d = body["hourly"]
            now = datetime.now().strftime("%Y-%m-%dT%H")
            for i, t in enumerate(d["time"]):
                if t[:13] >= now and t[:10] <= "2026-11-08" and (t[:10] < "2026-11-08" or t[11:13] <= "15"):
                    print(f"WX {t} t={d['temperature_2m'][i]} feel={d['apparent_temperature'][i]} wind={d['wind_speed_10m'][i]} "
                          f"dir={d['wind_direction_10m'][i]} mm={d['precipitation'][i]} pop={d['precipitation_probability'][i]} cloud={d['cloud_cover'][i]}", flush=True)
        except Exception as e:
            print("weather failed", e, flush=True)
        await asyncio.sleep(3600)


async def keepalive(app):
    """Free Render instances sleep after 15 min without inbound HTTP. Ping ourselves."""
    if not SELF_URL:
        return
    async with aiohttp.ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                async with s.get(SELF_URL + "/health", timeout=aiohttp.ClientTimeout(total=20)) as r:
                    await r.read()
            except Exception as e:
                print("keepalive failed", e, flush=True)


async def on_start(app):
    app["pool"] = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
    async with app["pool"].acquire() as c:
        await c.execute(SCHEMA)
    STATE["started"] = datetime.now(timezone.utc).isoformat()
    app["tasks"] = [asyncio.create_task(recorder(app)), asyncio.create_task(keepalive(app)),
                   asyncio.create_task(archiver(app)), asyncio.create_task(summary(app)), asyncio.create_task(weather(app))]


async def status(request):
    async with request.app["pool"].acquire() as c:
        n = {t: await c.fetchval(f"select count(*) from {t}") for t in ("raw", "laps", "pits", "events", "archive", "archive_rows")}
    lines = [f"Alcanede Apex recorder", f"started {STATE['started']}",
             f"connected: {STATE['connected'] or 'no'} {STATE['url'] or ''}",
             f"last message: {STATE['last_msg']}", f"messages this run: {STATE['msgs']}",
             f"stored: {n}", "recent tries:"] + STATE["tries"]
    return web.Response(text="\n".join(lines))


async def health(request):
    return web.Response(text="ok")


async def csv_out(request):
    t = request.match_info["t"]
    if t not in ("laps", "pits", "events", "archive_rows"):
        raise web.HTTPNotFound()
    async with request.app["pool"].acquire() as c:
        rows = await c.fetch(f"select * from {t} order by id")
    buf = io.StringIO(); w = csv.writer(buf)
    if rows:
        w.writerow(rows[0].keys())
        for r in rows:
            w.writerow(list(r.values()))
    return web.Response(text=buf.getvalue(), content_type="text/csv")


async def report(request):
    async with request.app["pool"].acquire() as c:
        laps = await c.fetch("select ts, session, kart, team, lap_s, s1, s2, s3 from laps")
    out = []
    by = defaultdict(list)
    for l in laps:
        by[l["session"]].append(l)
    for sess, ls in by.items():
        best = min(l["lap_s"] for l in ls)
        clean = [l for l in ls if l["lap_s"] < best * 1.25]
        ts = sorted(l["lap_s"] for l in clean)
        q = lambda p: ts[min(len(ts) - 1, int(p * len(ts)))]
        out.append(f"=== {sess or 'session'}: {len(ls)} laps, {len(clean)} clean ===")
        out.append(f"Best {ts[0]:.3f} | top 5% {q(.05):.3f} | top 25% {q(.25):.3f} | median {q(.5):.3f}")
        sb = []
        for s in ("s1", "s2", "s3"):
            v = sorted(x for x in (secs(l[s]) for l in clean) if x)
            sb.append(v)
            if v:
                out.append(f"  {s.upper()}: best {v[0]:.3f} | top 25% {v[int(.25 * len(v))]:.3f} | median {statistics.median(v):.3f}")
        if all(sb):
            out.append(f"  Ideal lap: {sum(v[0] for v in sb):.3f}")
        kart = defaultdict(list)
        for l in clean:
            kart[l["kart"]].append(l["lap_s"])
        out.append("  kart | laps | best | median of best 10")
        for k, v in sorted(kart.items(), key=lambda kv: statistics.median(sorted(kv[1])[:10])):
            out.append(f"  {k:>5} | {len(v):4} | {min(v):.3f} | {statistics.median(sorted(v)[:10]):.3f}")
    return web.Response(text="\n".join(out) or "No laps yet.")


def main():
    app = web.Application()
    app.on_startup.append(on_start)
    app.add_routes([web.get("/", status), web.get("/health", health), web.get("/report", report),
                    web.get("/{t}.csv", csv_out)])
    web.run_app(app, port=int(os.environ.get("PORT", "10000")))


if __name__ == "__main__":
    main()
