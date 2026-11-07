"""Team dashboard for car 56: stints, drivers, lap times, kart quality vs the field, live stint clock.

GET  /team        live dashboard (auto-refreshes)
GET  /team.json   the same data as JSON
POST /team/driver stint=<n>&driver=<name>&pin=<TEAM_PIN>   name who drove a stint
"""
import json, os, statistics
from collections import defaultdict
from datetime import datetime, timezone

from aiohttp import web

OUR = os.environ.get("OUR_NO", "56")
PIN = os.environ.get("TEAM_PIN", "jurassic")
SEED = os.environ.get("TEAM_DRIVERS", "1:Balikó,2:Lança,3:Luís Diogo,4:Carvalho,5:Ode,6:Ode")
RACE_SESSION = os.environ.get("RACE_SESSION", "Final")
RACE_HOURS = 26
STINT_MAX = 60 * 60

SCHEMA = "create table if not exists drivers (stint int primary key, driver text, ts timestamptz default now());"


async def setup(pool):
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        for pair in SEED.split(","):
            n, _, name = pair.partition(":")
            if n.strip().isdigit():
                await c.execute("insert into drivers(stint, driver) values($1,$2) on conflict do nothing", int(n), name.strip())


def med(xs):
    return statistics.median(xs) if xs else None


async def build(pool):
    async with pool.acquire() as c:
        laps = await c.fetch("select ts, kart, lap_s, pos, gap from laps where session=$1 and ts > now() - interval '30 hours' order by id", RACE_SESSION)
        pits = await c.fetch("select ts, event from pits where kart=$1 and session=$2 and ts > now() - interval '30 hours' order by id", OUR, RACE_SESSION)
        names = {r["stint"]: r["driver"] for r in await c.fetch("select stint, driver from drivers")}
    ours = [l for l in laps if l["kart"] == OUR]
    if not ours:
        return {"error": "no race laps for car %s yet" % OUR}
    # Green flag = first completed race lap minus its own lap time.
    from datetime import timedelta
    start = laps[0]["ts"] - timedelta(seconds=laps[0]["lap_s"])
    ins = [p["ts"] for p in pits if p["event"] == "in" and p["ts"] > start]
    outs = [p["ts"] for p in pits if p["event"] == "out" and p["ts"] > start]
    idx = lambda ts: sum(1 for t in ins if t < ts)   # stint index of a lap (0-based); the in-lap stays with its stint

    # Field reference: median and best clean lap of all other cars per 10-minute bucket.
    bucket = defaultdict(list)
    for l in laps:
        if l["kart"] != OUR and 40 < l["lap_s"] < 60:
            bucket[int((l["ts"] - start).total_seconds() // 600)].append(l["lap_s"])
    field = {b: med(v) for b, v in bucket.items()}
    field_best = {b: min(v) for b, v in bucket.items()}

    now = datetime.now(timezone.utc)
    stints, per_driver = [], defaultdict(list)
    for i in range(len(ins) + 1):
        ls = [l for l in ours if idx(l["ts"]) == i]
        t0 = start if i == 0 else (outs[i - 1] if i - 1 < len(outs) else ins[i - 1])
        t1 = ins[i] if i < len(ins) else None
        clean = sorted(l["lap_s"] for l in ls if 40 < l["lap_s"] < 60)
        deltas = [l["lap_s"] - field[k] for l in ls if 40 < l["lap_s"] < 60
                  for k in [int((l["ts"] - start).total_seconds() // 600)] if k in field]
        first5 = [l["lap_s"] for l in ls[1:6] if 40 < l["lap_s"] < 60]
        last5 = [l["lap_s"] for l in ls[-5:] if 40 < l["lap_s"] < 60]
        stop = (outs[i] - ins[i]).total_seconds() if i < len(ins) and i < len(outs) else None
        name = names.get(i + 1, "?")
        per_driver[name].append((clean, deltas, len(ls)))
        stints.append({
            "n": i + 1, "driver": name, "out": t0.isoformat(), "in": t1.isoformat() if t1 else None,
            "running": t1 is None, "length_s": round(((t1 or now) - t0).total_seconds()),
            "laps": len(ls), "best": clean[0] if clean else None,
            "avg10": round(statistics.mean(clean[:10]), 3) if clean else None,
            "median": round(med(clean), 3) if clean else None,
            "sd": round(statistics.pstdev(clean[: max(2, int(len(clean) * 0.9))]), 3) if len(clean) > 2 else None,
            "vs_field": round(med(deltas), 3) if deltas else None,
            "fade": round(med(last5) - med(first5), 3) if first5 and last5 and len(ls) > 12 else None,
            "pos_end": ls[-1]["pos"] if ls else None, "stop_s": round(stop) if stop else None,
        })
    dsum = []
    for name, rows in per_driver.items():
        xs = sorted(x for c, _, _ in rows for x in c)
        vs = [x for _, d, _ in rows for x in d]
        dsum.append({"driver": name, "stints": len(rows), "laps": sum(n for _, _, n in rows), "best": xs[0] if xs else None,
                     "avg10": round(statistics.mean(xs[:10]), 3) if xs else None, "median": round(med(xs), 3) if xs else None,
                     "vs_field": round(med(vs), 3) if vs else None})
    dsum.sort(key=lambda d: d["avg10"] or 99)
    cur = stints[-1]
    cur_out = datetime.fromisoformat(cur["out"])
    return {
        "team": OUR, "now": now.isoformat(), "race_start": start.isoformat(),
        "race_end": (start.timestamp() + RACE_HOURS * 3600),
        "stops_done": len(ins), "stints": stints, "drivers": dsum,
        "current": {"n": cur["n"], "driver": cur["driver"], "out": cur["out"],
                    "elapsed_s": round((now - cur_out).total_seconds()),
                    "limit": cur_out.timestamp() + STINT_MAX},
        "laps": [[l["ts"].isoformat(), l["lap_s"], l["pos"], l["gap"]] for l in ours],
        "field": [[(start.timestamp() + b * 600 + 300) * 1000, field[b], field_best[b]] for b in sorted(field)],
        "last": {"lap": ours[-1]["lap_s"], "pos": ours[-1]["pos"], "gap": ours[-1]["gap"], "ts": ours[-1]["ts"].isoformat()},
    }


async def team_json(request):
    return web.json_response(await build(request.app["pool"]), dumps=lambda o: json.dumps(o, default=str))


async def set_driver(request):
    data = await request.post()
    if data.get("pin") != PIN:
        raise web.HTTPForbidden(text="wrong pin")
    n = int(data.get("stint", "0"))
    name = (data.get("driver") or "").strip()[:40]
    if n < 1 or not name:
        raise web.HTTPBadRequest(text="stint and driver needed")
    async with request.app["pool"].acquire() as c:
        await c.execute("insert into drivers(stint, driver) values($1,$2) on conflict (stint) do update set driver=excluded.driver, ts=now()", n, name)
    raise web.HTTPFound("/team")


async def team_page(request):
    return web.Response(text=PAGE, content_type="text/html")


PAGE = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Car 56 · 26H Alcanede</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root{--ink:#151515;--bg:#f4f2ee;--acc:#d7261e;--mute:#666}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px system-ui,-apple-system,sans-serif;padding:12px}
h1{font-size:22px;margin:0 0 2px;text-transform:uppercase;letter-spacing:.5px}h2{font-size:15px;text-transform:uppercase;margin:18px 0 6px;border-bottom:2px solid var(--ink);padding-bottom:3px}
.sub{color:var(--mute);font-size:13px}
.big{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:10px}
.big div{background:#fff;border:2px solid var(--ink);padding:8px}.big span{display:block;font-size:11px;color:var(--mute);text-transform:uppercase}
.big b{font:700 22px ui-monospace,Menlo,monospace}.warnb{background:#fff1a8!important}.redb{background:#ffd6d2!important}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}th,td{padding:5px 4px;border-bottom:1px solid #ddd;text-align:right;white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}th{background:#151515;color:#fff;font-weight:600}
.scroll{overflow-x:auto;border:2px solid var(--ink)}.good{color:#1b7f3a;font-weight:700}.bad{color:var(--acc);font-weight:700}
canvas{background:#fff;border:2px solid var(--ink);margin-top:4px}
form{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}form input,form select,form button{font-size:15px;padding:6px;border:2px solid var(--ink)}form button{background:var(--ink);color:#fff}
.note{font-size:12px;color:var(--mute);margin-top:6px}
</style></head><body>
<h1>Car 56 · Jurassic Kart Team</h1><div class="sub" id="sub">loading…</div>
<div class="big" id="big"></div>
<h2>Stints</h2><div class="scroll"><table id="st"></table></div>
<div class="note">vs field = median lap minus the median of all other cars in the same 10 min (negative = faster). fade = last 5 laps minus laps 2-6. sd = lap consistency.</div>
<h2>Drivers</h2><div class="scroll"><table id="dr"></table></div>
<h2>Lap times</h2><canvas id="c1" height="230"></canvas>
<h2>Position</h2><canvas id="c2" height="150"></canvas>
<h2>Who drove</h2>
<form method="post" action="/team/driver"><select name="stint" id="stsel"></select><input name="driver" placeholder="Driver" list="dl" required><datalist id="dl"><option>Balikó<option>Lança<option>Luís Diogo<option>Carvalho<option>Ode</datalist><input name="pin" placeholder="PIN" type="password" required style="width:90px"><button>Save</button></form>
<script>
const COL={"Balikó":"#d7261e","Lança":"#b8860b","Luís Diogo":"#8e44ad","Carvalho":"#2e9e44","Ode":"#1f6feb","?":"#888"};
let c1,c2;
const t=s=>new Date(s).toLocaleTimeString('pt-PT',{hour:'2-digit',minute:'2-digit'});
const mmss=s=>s==null?'':Math.floor(s/60)+':'+String(Math.round(s%60)).padStart(2,'0');
const f=(v,d=3)=>v==null?'':(+v).toFixed(d);
const sg=v=>v==null?'':'<span class="'+(v<0?'good':v>0.3?'bad':'')+'">'+(v>0?'+':'')+(+v).toFixed(2)+'</span>';
async function load(){
  const d=await (await fetch('/team.json')).json();
  if(d.error){document.getElementById('sub').textContent=d.error;return;}
  const now=Date.now(),cur=d.current,left=cur.limit?cur.limit*1000-now:null;
  document.getElementById('sub').textContent='Green flag '+t(d.race_start)+' · flag '+t(d.race_end*1000)+' Sun · stops '+d.stops_done+'/34 · updated '+t(d.now);
  const cls=left!=null&&left<5*60e3?'redb':left!=null&&left<10*60e3?'warnb':'';
  document.getElementById('big').innerHTML=
    '<div><span>Now driving</span><b>'+cur.driver+'</b></div>'+
    '<div class="'+cls+'"><span>Stint '+cur.n+' clock · box by</span><b>'+mmss(cur.elapsed_s)+'</b> · '+(cur.limit?t((cur.limit-180)*1000):'')+'</div>'+
    '<div><span>Last lap</span><b>'+f(d.last.lap)+'</b></div><div><span>Position · gap</span><b>P'+d.last.pos+'</b> '+(d.last.gap||'')+'</div>';
  document.getElementById('st').innerHTML='<tr><th>#</th><th>Driver</th><th>Out</th><th>Len</th><th>Laps</th><th>Best</th><th>Avg10</th><th>Median</th><th>sd</th><th>vs field</th><th>Fade</th><th>Stop</th><th>Pos</th></tr>'+
    d.stints.map(s=>'<tr><td>'+s.n+'</td><td><b style="color:'+(COL[s.driver]||'#333')+'">'+s.driver+'</b></td><td>'+(s.out?t(s.out):'')+'</td><td>'+mmss(s.length_s)+(s.running?' ▶':'')+'</td><td>'+s.laps+'</td><td>'+f(s.best)+'</td><td>'+f(s.avg10)+'</td><td>'+f(s.median)+'</td><td>'+f(s.sd,2)+'</td><td>'+sg(s.vs_field)+'</td><td>'+sg(s.fade)+'</td><td>'+mmss(s.stop_s)+'</td><td>'+(s.pos_end||'')+'</td></tr>').join('');
  document.getElementById('dr').innerHTML='<tr><th>Driver</th><th></th><th>Stints</th><th>Laps</th><th>Best</th><th>Avg10</th><th>Median</th><th>vs field</th></tr>'+
    d.drivers.map(x=>'<tr><td><b style="color:'+(COL[x.driver]||'#333')+'">'+x.driver+'</b></td><td></td><td>'+x.stints+'</td><td>'+x.laps+'</td><td>'+f(x.best)+'</td><td>'+f(x.avg10)+'</td><td>'+f(x.median)+'</td><td>'+sg(x.vs_field)+'</td></tr>').join('');
  document.getElementById('stsel').innerHTML=d.stints.map(s=>'<option value="'+s.n+'"'+(s.running?' selected':'')+'>Stint '+s.n+' ('+s.driver+')</option>').join('');
  // stint index per lap
  const ins=d.stints.filter(s=>s.in).map(s=>Date.parse(s.in));
  const pts=d.laps.filter(l=>l[1]>40&&l[1]<60).map(l=>{const ts=Date.parse(l[0]);const k=ins.filter(x=>x<ts).length;return {x:ts,y:l[1],drv:(d.stints[k]||{}).driver||'?'};});
  const byDrv={};pts.forEach(p=>{(byDrv[p.drv]=byDrv[p.drv]||[]).push(p);});
  const ds=Object.keys(byDrv).map(k=>({type:'scatter',label:k,data:byDrv[k],pointRadius:2,backgroundColor:COL[k]||'#888'}));
  ds.push({type:'line',label:'Field median',data:d.field.map(r=>({x:r[0],y:r[1]})),borderColor:'#151515',borderWidth:1.5,pointRadius:0,borderDash:[5,4]});
  ds.push({type:'line',label:'Field best',data:d.field.map(r=>({x:r[0],y:r[2]})),borderColor:'#999',borderWidth:1,pointRadius:0});
  const ys=pts.map(p=>p.y).sort((a,b)=>a-b),lo=ys.length?Math.floor(ys[0]*2)/2-0.5:45,hi=ys.length?Math.min(ys[Math.floor(ys.length*0.98)]+1,60):55;
  const opt={animation:false,responsive:true,plugins:{legend:{labels:{boxWidth:10,font:{size:11}}}},scales:{x:{type:'time',time:{unit:'hour',displayFormats:{hour:'HH:mm'}}},y:{min:lo,max:hi}}};
  if(c1)c1.destroy();c1=new Chart(document.getElementById('c1'),{data:{datasets:ds},options:opt});
  const pos=d.laps.map(l=>({x:Date.parse(l[0]),y:+l[2]})).filter(p=>p.y);
  if(c2)c2.destroy();c2=new Chart(document.getElementById('c2'),{type:'line',data:{datasets:[{label:'Position',data:pos,borderColor:'#d7261e',pointRadius:0,stepped:true}]},options:{animation:false,plugins:{legend:{display:false}},scales:{x:{type:'time',time:{unit:'hour',displayFormats:{hour:'HH:mm'}}},y:{reverse:true,min:1,ticks:{stepSize:1}}}}});
}
load();setInterval(load,30000);
</script></body></html>"""
