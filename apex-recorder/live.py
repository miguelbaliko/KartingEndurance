"""Live timing and virtual classification.

GET /live       phone-friendly timing screen, refreshes every 5 s
GET /live.json  the same data

Virtual position: real progress minus the time each team still owes in mandatory stops
(PRO 34, AM 40), so a team that has stopped more is not behind in reality.
"""
import json, os
from datetime import datetime, timezone

from aiohttp import web

PRO_STOPS, AM_STOPS = 34, 40
AM_KARTS = set(os.environ.get("AM_KARTS", "64").split(","))
MARK = set(os.environ.get("TEAMS", "56,64").split(","))
STOP_LOSS = float(os.environ.get("STOP_LOSS_S", "213.5"))   # measured: stop lap 260.7 s - normal lap 47.3 s
RACE_SESSION = os.environ.get("RACE_SESSION", "Final")


def cls_of(row, no):
    for v in row.values():
        if v in ("AM", "PRO"):
            return v
    return "AM" if no in AM_KARTS else "PRO"


async def build(app):
    feed = app.get("feed")
    rows = dict(feed.rows) if feed else {}
    pool = app["pool"]
    async with pool.acquire() as c:
        laps = {r["kart"]: (r["n"], r["best"], r["ref"]) for r in await c.fetch(
            "select kart, count(*) n, min(lap_s) filter (where lap_s > 40) best, "
            "percentile_cont(0.5) within group (order by lap_s) filter (where lap_s between 40 and 60) ref "
            "from laps where session=$1 and ts > now() - interval '30 hours' group by kart", RACE_SESSION)}
        t0 = await c.fetchval("select min(ts - make_interval(secs => lap_s)) from laps where session=$1 and ts > now() - interval '30 hours'", RACE_SESSION)
        pits = await c.fetch("select kart, event, ts from pits where session=$1 and ts > now() - interval '30 hours' order by id", RACE_SESSION)
    stops, last_ev = {}, {}
    for p in pits:
        if p["event"] == "in":
            stops[p["kart"]] = stops.get(p["kart"], 0) + 1
        last_ev[p["kart"]] = (p["event"], p["ts"])
    now = datetime.now(timezone.utc)
    race_s = (now - t0).total_seconds() if t0 else 0
    left_s = max(0, 26 * 3600 - race_s)
    refs = sorted(v[2] for v in laps.values() if v[2])
    ref = refs[len(refs) // 2] if refs else 47.5
    out = []
    for r, st in rows.items():
        no = st.get("no", "")
        if not no:
            continue
        n, best, _ = laps.get(no, (0, None, None))
        cl = cls_of(st, no)
        req = AM_STOPS if cl == "AM" else PRO_STOPS
        done = stops.get(no, 0)
        ev, ts = last_ev.get(no, (None, None))
        in_pit = ev == "in"
        stint = (now - ts).total_seconds() if ts else None
        owed = max(0, req - done)
        avg_stint = (race_s - done * 213) / (done + 1) if race_s else None
        need = (left_s - 1200 - owed * 213) / owed if owed else None
        pos = int(st["rk"]) if st.get("rk", "").isdigit() else 999
        out.append({"pos": pos, "no": no, "team": st.get("dr", ""), "cls": cl, "laps": n,
                    "last": st.get("llp", ""), "best": round(best, 3) if best else None, "gap": st.get("gap", ""),
                    "int": st.get("int", ""), "stops": done, "req": req, "owed": owed,
                    "in_pit": in_pit, "since": round(stint) if stint is not None else None,
                    "vlaps": n - owed * STOP_LOSS / ref, "mark": no in MARK,
                    "avg_stint": round(avg_stint) if avg_stint else None, "need_stint": round(need) if need else None})
    out.sort(key=lambda x: x["pos"])
    for i, x in enumerate(sorted(out, key=lambda x: (-x["vlaps"], x["pos"]))):
        x["vpos"] = i + 1
    lead = max((x["vlaps"] for x in out), default=0)
    for x in out:
        x["vgap_s"] = round((lead - x["vlaps"]) * ref, 1)
        x["vlaps"] = round(x["vlaps"], 2)
    for c in ("PRO", "AM"):
        for i, x in enumerate(sorted([x for x in out if x["cls"] == c], key=lambda x: x["pos"])):
            x["cpos"] = i + 1
        for i, x in enumerate(sorted([x for x in out if x["cls"] == c], key=lambda x: x["vpos"])):
            x["cvpos"] = i + 1
    return {"now": now.isoformat(), "session": feed.session if feed else "", "flag": feed.flag if feed else "",
            "ref_lap": round(ref, 3), "stop_loss_s": STOP_LOSS, "rows": out}


async def live_json(request):
    return web.json_response(await build(request.app), dumps=lambda o: json.dumps(o, default=str),
                             headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})


async def live_page(request):
    return web.Response(text=PAGE, content_type="text/html")


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Live · 26H Alcanede</title>
<style>
:root{--bg:#0e1110;--card:#171c1a;--line:#26302c;--ink:#e8ede9;--mut:#8a978f;--acc:#c6f432;--bad:#ff5a4e;--good:#3ddc84;--warn:#ffb020}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.35 ui-monospace,Menlo,Consolas,monospace}
header{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:3;flex-wrap:wrap}
h1{margin:0;font:700 18px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.03em}h1 b{color:var(--acc)}
.bt{display:flex;gap:4px}.bt button{background:var(--card);color:var(--mut);border:1px solid var(--line);padding:6px 10px;border-radius:5px;font:inherit;cursor:pointer}
.bt button.on{background:var(--acc);color:#111;border-color:var(--acc);font-weight:700}
.st{color:var(--mut);font-size:11px;width:100%}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px 12px}
.c{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px}.c .t{color:var(--mut);font-size:10px;text-transform:uppercase}
.c .n{font:700 26px system-ui,sans-serif}.c .s{color:var(--mut);font-size:11px}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;white-space:nowrap}
th,td{padding:6px 7px;border-bottom:1px solid var(--line);text-align:right}th{color:var(--mut);font-weight:400;font-size:10px;text-transform:uppercase;position:sticky;top:0;background:var(--bg)}
td.l,th.l{text-align:left}tr.m td{background:#1f2a12}tr.m td.l{color:var(--acc);font-weight:700}
.pit{color:var(--warn)}.up{color:var(--good)}.dn{color:var(--bad)}.am{color:#4cc9f0}.mut{color:var(--mut)}
.note{color:var(--mut);font-size:11px;padding:10px 12px}
</style></head><body>
<header><h1>Live <b>26H</b></h1><div class="bt" id="view"><button data-v="real" class="on">Real</button><button data-v="virt">Virtual</button></div>
<div class="bt" id="cls"><button data-c="ALL" class="on">All</button><button data-c="PRO">PRO</button><button data-c="AM">AM</button></div><div class="st" id="st"></div></header>
<div class="cards" id="cards"></div>
<div class="wrap"><table id="t"></table></div>
<div class="note">Virtual = laps done minus the stops each team still owes (PRO 34, AM 40) × <span id="sl"></span> s, at the field median lap <span id="rl"></span> s. It shows who is really ahead once everyone has made all their stops. ▲▼ = virtual vs real. Stint = time since last pit exit (or in the pit since).</div>
<script>
let V="real",C="ALL",D=null;
const mmss=s=>s==null?"–":Math.floor(s/60)+":"+String(Math.round(s%60)).padStart(2,"0");
document.querySelectorAll("#view button").forEach(b=>b.onclick=()=>{V=b.dataset.v;document.querySelectorAll("#view button").forEach(x=>x.classList.toggle("on",x==b));draw()});
document.querySelectorAll("#cls button").forEach(b=>b.onclick=()=>{C=b.dataset.c;document.querySelectorAll("#cls button").forEach(x=>x.classList.toggle("on",x==b));draw()});
function draw(){if(!D)return;
 let rows=D.rows.filter(r=>C=="ALL"||r.cls==C);
 const P=r=>C=="ALL"?r.pos:r.cpos, VP=r=>C=="ALL"?r.vpos:r.cvpos;
 rows.sort((a,b)=>V=="real"?P(a)-P(b):VP(a)-VP(b));
 document.getElementById("cards").innerHTML=D.rows.filter(r=>r.mark).map(r=>`<div class="c"><div class="t">#${r.no} ${r.cls} · ${r.team}</div><div class="n">P${r.cpos} <span class="s">${r.cls}</span> · V${r.cvpos}</div><div class="s">overall P${r.pos} / V${r.vpos} · stops ${r.stops}/${r.req} · ${r.in_pit?'<span class=pit>IN PIT '+mmss(r.since)+'</span>':'stint '+mmss(r.since)}</div><div class="s">last ${r.last||"–"} · best ${r.best??"–"} · gap ${r.gap||"–"}</div></div>`).join("");
 document.getElementById("t").innerHTML=`<tr><th>${V=="real"?"Pos":"V"}</th><th>${V=="real"?"V":"Pos"}</th><th class=l>#</th><th class=l>Team</th><th>Cls</th><th>Laps</th><th>Last</th><th>Best</th><th>${V=="real"?"Gap":"V gap"}</th><th>Stops</th><th>Avg st</th><th>Need</th><th>Stint</th></tr>`+
 rows.map(r=>{const a=V=="real"?P(r):VP(r),b=V=="real"?VP(r):P(r),d=P(r)-VP(r);
  return `<tr class="${r.mark?"m":""}"><td><b>${a}</b></td><td class="${V=="real"?(d>0?"up":d<0?"dn":"mut"):"mut"}">${b}${V=="real"&&d?(d>0?"▲":"▼"):""}</td><td class=l>${r.no}</td><td class=l>${r.team.slice(0,22)}</td><td class="${r.cls=="AM"?"am":""}">${r.cls}</td><td>${r.laps}</td><td>${r.last||"–"}</td><td>${r.best??"–"}</td><td>${V=="real"?(r.gap||"–"):(r.vgap_s?"+"+r.vgap_s.toFixed(1):"–")}</td><td>${r.stops}/${r.req}</td><td>${mmss(r.avg_stint)}</td><td class="${r.need_stint<r.avg_stint-120?"dn":""}">${mmss(r.need_stint)}</td><td class="${r.in_pit?"pit":r.since>3300?"dn":""}">${r.in_pit?"PIT "+mmss(r.since):mmss(r.since)}</td></tr>`}).join("");
 document.getElementById("st").textContent=`${D.session} · flag ${D.flag||"–"} · ${new Date(D.now).toLocaleTimeString("pt-PT")}`;
 document.getElementById("sl").textContent=D.stop_loss_s;document.getElementById("rl").textContent=D.ref_lap;}
async function load(){try{D=await (await fetch("live.json",{cache:"no-store"})).json();draw()}catch(e){document.getElementById("st").textContent="reconnecting…"}}
load();setInterval(load,5000);
</script></body></html>"""


def compact(d):
    import base64, zlib
    rows = [[x["no"], x["team"][:22], x["cls"], x["pos"], x["laps"], x["stops"], x["req"], x["vpos"], x["vgap_s"],
             int(x["in_pit"]), x["since"], x["best"], x["avg_stint"], x["need_stint"], x.get("cpos"), x.get("cvpos")] for x in d["rows"]]
    blob = json.dumps({"now": d["now"], "ref": d["ref_lap"], "loss": d["stop_loss_s"], "rows": rows}, separators=(",", ":"), default=str)
    return base64.b64encode(zlib.compress(blob.encode(), 9)).decode()
