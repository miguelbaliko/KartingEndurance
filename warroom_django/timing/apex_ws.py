"""
Apex Timing live data client.

Two transport modes:
  1. WebSocket  — wss://www.apex-timing.com:{port+3}/  (real-time)
  2. AJAX poll  — https://live.apex-timing.com/commonv2/functions/live_ajax.php  (fallback)

Both return the same pipe-delimited message format.

Grid message cell classes (confirmed from live JS):
  c3=ranking  c4=kart#  c5=driver  c6=last_lap  c7=best_lap  c8=gap  c9=laps  c10=pits
Rows:  <tr data-id="r{kart}" data-pos="{position}">
"""

import logging
import random
import re
import ssl
import string
import threading
import time
from html.parser import HTMLParser
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_thread  = None
_running = False

# ─── Constants ────────────────────────────────────────────────────────────────

# Maps both td CSS class names (data rows) and c-number data-id values → field
_CELL_MAP = {
    # Semantic class names on <td> elements in data rows (confirmed from live HTML)
    'rk':  'position',
    'no':  'kart',
    'dr':  'name',
    'llp': 'last_lap',
    'blp': 'best_lap',
    'gap': 'gap',
    'tlp': 'laps',
    'pit': 'pits',
    'int': 'interval',
    # c-number fallback (header row data-id values)
    'c3': 'position',
    'c4': 'kart',
    'c5': 'name',
    'c6': 'last_lap',
    'c7': 'best_lap',
    'c8': 'gap',
    'c9': 'laps',
    'c10': 'pits',
    'c11': 'interval',
}

# In-memory caches
_row_cache: dict[str, dict] = {}          # kart_number → row dict
_iid_to_kart: dict[str, str] = {}         # internal_id (r28) → kart number (36)
_cache_lock = threading.Lock()


# ─── Value helpers ────────────────────────────────────────────────────────────

def _strip(text: str) -> str:
    """Remove color codes and whitespace from a timing value."""
    if not text:
        return ''
    text = re.sub(r'#[0-9A-Fa-f]{3,6}', '', text)   # hex colours
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _lap_to_seconds(value) -> float | None:
    """'46.512' | '1:26.512' | '46512'(ms) → float seconds."""
    if not value:
        return None
    s = _strip(str(value)).lstrip('+')
    if not s or s in ('-', '—'):
        return None
    try:
        if ':' in s:
            parts = s.split(':', 1)
            return float(parts[0]) * 60 + float(parts[1])
        f = float(s)
        return f / 1000 if f > 1000 else f   # milliseconds guard
    except ValueError:
        return None


def _int(value, default=0) -> int:
    try:
        return int(re.sub(r'[^\d]', '', str(value)) or default)
    except (ValueError, TypeError):
        return default


# ─── Grid HTML parser ─────────────────────────────────────────────────────────

class _GridParser(HTMLParser):
    """
    Parse the HTML fragment sent in Apex 'grid' messages.

    Confirmed live structure (kartalcanede):
        <tr data-id="r28" data-pos="1">
            <td data-id="r28c1" class="gs"></td>           ← group/status (skip)
            <td data-id="r28c2" class="in"></td>           ← driver status (skip)
            <td class="rk"><div><p data-id="r28c3">1</p></div></td>   ← ranking
            <td class="no"><div data-id="r28c4" class="no1">36</div></td> ← kart#
            <td data-id="r28c5" class="dr">NUNO FORMIGO</td>          ← driver
            <td data-id="r28c6" class="llp">46.512</td>               ← last lap
            ...
        </tr>

    Key insight: cells use semantic class names (rk, no, dr, llp, blp, gap, tlp, pit).
    data-id on <td> is row-prefixed (r28c5) and NOT useful for field mapping.
    Position comes from data-pos on <tr>; kart number from class="no" cell content.
    """

    def __init__(self):
        super().__init__()
        self.teams: list[dict] = []
        self._row:  dict | None = None
        self._cls:  str  | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'tr':
            dpos = a.get('data-pos', '0')
            did  = a.get('data-id', '')
            if dpos and dpos != '0':
                iid = did.lstrip('rR')   # internal ID (e.g. "28") — NOT the kart number
                self._row = {'_iid': iid, 'position': _int(dpos, 0)}
        elif tag == 'td' and self._row is not None:
            cls = a.get('class', '')
            # Pick the first class word that's in our map (semantic names take priority)
            matched = next((c for c in cls.split() if c in _CELL_MAP), None)
            if matched:
                self._cls = matched
            else:
                # Fall back to data-type (header rows) or skip
                dt = a.get('data-type', '')
                self._cls = dt if dt in _CELL_MAP else None

    def handle_endtag(self, tag):
        if tag == 'tr' and self._row:
            # Fall back to internal ID if no explicit kart number from class="no" cell
            if not self._row.get('kart') and self._row.get('_iid'):
                self._row['kart'] = self._row['_iid']
            if self._row.get('kart'):
                self.teams.append(self._row)
            self._row = None
        elif tag == 'td':
            self._cls = None

    def handle_data(self, data):
        data = _strip(data)
        if not data or not self._cls or self._row is None:
            return
        field = _CELL_MAP.get(self._cls)
        if not field:
            return
        # Always overwrite position/kart (cell value is more reliable than data-pos/data-id)
        if field in ('position', 'kart') or field not in self._row:
            self._row[field] = data


# ─── Message processor ────────────────────────────────────────────────────────

def _process_line(parts: list[str], session):
    """Handle one pipe-split message line."""
    if not parts or not parts[0]:
        return

    msg_type = parts[0]

    # ── Full grid refresh ────────────────────────────────────────────────────
    if msg_type == 'grid' and len(parts) >= 3:
        html = parts[2]
        parser = _GridParser()
        parser.feed(html)
        if parser.teams:
            with _cache_lock:
                for row in parser.teams:
                    kart = row.get('kart', '')
                    iid  = row.get('_iid', '')
                    if kart:
                        _row_cache[kart] = row
                        if iid:
                            _iid_to_kart[iid] = kart   # internal_id → real kart#
            _flush_cache_to_db(session)
        return

    # ── Pit in / out ─────────────────────────────────────────────────────────
    if msg_type in ('*in', '*out') and len(parts) >= 2:
        _handle_pit(parts[1].strip(), 'IN' if msg_type == '*in' else 'OUT', session)
        return

    # ── Individual cell update: "{iid}|{field_code}|{value}" ─────────────────
    # iid is the internal row ID (e.g. "28" or "r28"); resolve to real kart#
    iid_raw = msg_type.lstrip('rR')
    if iid_raw.isdigit() and len(parts) >= 3:
        field_code = parts[1]
        value      = _strip(parts[2])
        field_name = _CELL_MAP.get(field_code)
        if field_name and value:
            with _cache_lock:
                # Resolve internal ID to real kart number
                kart = _iid_to_kart.get(iid_raw, iid_raw)
                if kart not in _row_cache:
                    _row_cache[kart] = {'kart': kart}
                _row_cache[kart][field_name] = value
            if field_code in ('llp', 'c6'):   # new lap → trigger strategy
                _flush_cache_to_db(session)


def _process_messages(raw: str, session):
    """Split a raw payload and dispatch each line."""
    for line in raw.replace('\r', '').split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        try:
            _process_line(parts, session)
        except Exception:
            logger.debug(f"Failed to process line: {line[:80]}", exc_info=True)


# ─── DB helpers ───────────────────────────────────────────────────────────────

def _db_reset():
    import django.db
    django.db.close_old_connections()


def _flush_cache_to_db(session):
    """Write current _row_cache snapshot to DB."""
    _db_reset()
    from timing.models import TeamSnapshot, LapRecord

    our_name = session.team_name.lower()
    with _cache_lock:
        snapshot = dict(_row_cache)

    for kart, row in snapshot.items():
        if not kart:
            continue

        name      = _strip(row.get('name') or f'#{kart}')
        pos       = _int(row.get('position', 0))
        last_lap  = _lap_to_seconds(row.get('last_lap'))
        best_lap  = _lap_to_seconds(row.get('best_lap'))
        lap_count = _int(row.get('laps', 0))
        pit_count = _int(row.get('pits', 0))
        gap       = _strip(row.get('gap', ''))
        interval  = _strip(row.get('interval', ''))
        is_ours   = our_name in name.lower() or our_name in kart.lower()

        team, _ = TeamSnapshot.objects.update_or_create(
            session=session,
            kart_number=kart,
            defaults=dict(
                name=name,
                position=pos,
                last_lap_seconds=last_lap,
                best_lap_seconds=best_lap,
                lap_count=lap_count,
                pit_count=pit_count,
                gap=gap,
                interval=interval,
                is_our_team=is_ours,
            ),
        )

        if last_lap and lap_count > 0:
            LapRecord.objects.update_or_create(
                team=team,
                lap_number=lap_count,
                defaults={'lap_time_seconds': last_lap},
            )

    try:
        from timing.strategy import compute_strategy
        compute_strategy(session)
    except Exception:
        logger.debug("Strategy compute skipped", exc_info=True)


def _handle_pit(kart: str, event_type: str, session):
    _db_reset()
    from timing.models import TeamSnapshot, PitEvent, RaceEvent
    try:
        team = TeamSnapshot.objects.get(session=session, kart_number=kart)
        PitEvent.objects.create(team=team, event_type=event_type, lap_number=team.lap_count)
        if team.is_our_team:
            RaceEvent.objects.create(
                session=session, level='INFO', category='TIMING',
                message=f'Our kart #{kart} pit {event_type} at lap {team.lap_count}',
            )
    except TeamSnapshot.DoesNotExist:
        pass


# ─── Config fetch ─────────────────────────────────────────────────────────────

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_config_port(base_url: str) -> int:
    try:
        url = base_url.rstrip('/') + '/javascript/config.js'
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, context=_ssl_ctx(), timeout=10) as r:
            text = r.read().decode('utf-8', errors='ignore')
        m = re.search(r'var\s+configPort\s*=\s*(\d+)', text)
        if m:
            port = int(m.group(1))
            logger.info(f'Apex configPort = {port}')
            return port
    except Exception as e:
        logger.warning(f'Could not read config.js: {e}')
    return 9740


# ─── AJAX fallback ────────────────────────────────────────────────────────────

_AJAX_URL = 'https://live.apex-timing.com/commonv2/functions/live_ajax.php'


def _ajax_worker(apex_url: str):
    global _running
    _db_reset()

    port      = _fetch_config_port(apex_url)
    sess_id   = ''.join(random.choices(string.digits, k=8))
    counter   = 0

    logger.info(f'AJAX fallback polling {_AJAX_URL}')

    while _running:
        try:
            from timing.models import RaceSession
            session = RaceSession.objects.filter(mode='LIVE').order_by('-created_at').first()
            if not session:
                time.sleep(5)
                continue

            params = urlencode({
                'version': '2.0.0',
                'init':    '1' if counter == 0 else '0',
                'index':   str(counter),
                'port':    str(port + 4),
                'counter': str(counter),
                'duration': str(counter * 2),
                'id':      sess_id,
                'ignored': '0',
            }).encode()

            req = Request(
                _AJAX_URL, data=params,
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': apex_url,
                    'Origin': 'https://live.apex-timing.com',
                },
            )
            with urlopen(req, context=_ssl_ctx(), timeout=10) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')

            # Response batches are separated by @
            for batch in raw.split('@'):
                _process_messages(batch, session)

            counter += 1
            _db_reset()

        except Exception as e:
            logger.error(f'AJAX error: {e}')

        time.sleep(2)


# ─── WebSocket transport ──────────────────────────────────────────────────────

def _ws_worker(apex_url: str):
    global _running
    import websocket

    port   = _fetch_config_port(apex_url)
    ws_url = f'wss://www.apex-timing.com:{port + 3}/'
    logger.info(f'Apex WS → {ws_url}')

    def on_open(ws):
        logger.info('Apex WS connected — sending init')
        ws.send('init')

    def on_message(ws, raw):
        _db_reset()
        from timing.models import RaceSession
        session = RaceSession.objects.filter(mode='LIVE').order_by('-created_at').first()
        if session:
            _process_messages(raw, session)

    def on_error(ws, err):
        logger.error(f'Apex WS error: {err}')

    def on_close(ws, code, msg):
        logger.warning(f'Apex WS closed ({code})')

    backoff = 5
    while _running:
        try:
            ws = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(sslopt={'cert_reqs': ssl.CERT_NONE}, ping_interval=30)
        except Exception:
            logger.exception('WS exception')

        if _running:
            logger.info(f'WS reconnect in {backoff}s')
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    logger.info('Apex WS worker stopped')


# ─── Public API ───────────────────────────────────────────────────────────────

def start_apex_thread(use_ajax=False):
    global _thread, _running

    _db_reset()
    try:
        from timing.models import RaceSession
        session = RaceSession.objects.filter(mode='LIVE').order_by('-created_at').first()
        apex_url = session.apex_url if session else 'https://live.apex-timing.com/kartalcanede/'
    except Exception:
        apex_url = 'https://live.apex-timing.com/kartalcanede/'

    _running = True
    target   = _ajax_worker if use_ajax else _ws_worker
    name     = 'apex-ajax' if use_ajax else 'apex-ws'

    _thread = threading.Thread(target=target, args=(apex_url,), daemon=True, name=name)
    _thread.start()
    logger.info(f'Apex thread [{name}] started → {apex_url}')


def stop_apex_thread():
    global _running
    _running = False
