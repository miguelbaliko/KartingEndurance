"""
Management command to test Apex Timing connectivity and print raw messages.
Usage:
    python manage.py test_apex
    python manage.py test_apex --ajax          # use HTTP fallback instead of WS
    python manage.py test_apex --seconds 60    # run for 60 seconds (default 30)
"""
import ssl
import re
import time
import threading
from django.core.management.base import BaseCommand
from urllib.request import Request, urlopen


class Command(BaseCommand):
    help = 'Test Apex Timing connection and print live data for N seconds'

    def add_arguments(self, parser):
        parser.add_argument('--url', default='https://live.apex-timing.com/kartalcanede/')
        parser.add_argument('--seconds', type=int, default=30)
        parser.add_argument('--ajax', action='store_true', help='Use AJAX polling instead of WebSocket')

    def handle(self, *args, **options):
        apex_url  = options['url']
        duration  = options['seconds']
        use_ajax  = options['ajax']

        self.stdout.write(self.style.SUCCESS(f'\n=== Apex Timing Test ==='))
        self.stdout.write(f'URL:  {apex_url}')
        self.stdout.write(f'Mode: {"AJAX" if use_ajax else "WebSocket"}')
        self.stdout.write(f'Running for {duration}s — Ctrl+C to stop\n')

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Fetch configPort
        port = 9740
        try:
            cfg_url = apex_url.rstrip('/') + '/javascript/config.js'
            with urlopen(Request(cfg_url, headers={'User-Agent': 'Mozilla/5.0'}), context=ctx, timeout=8) as r:
                text = r.read().decode('utf-8', errors='ignore')
            m = re.search(r'var\s+configPort\s*=\s*(\d+)', text)
            if m:
                port = int(m.group(1))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'config.js fetch failed: {e}'))

        self.stdout.write(f'configPort = {port}')

        stop_event = threading.Event()

        def on_message(raw: str):
            for line in raw.replace('\r', '').split('\n'):
                line = line.strip()
                if not line:
                    continue
                parts = line.split('|')
                msg_type = parts[0]

                if msg_type == 'grid':
                    html = parts[2] if len(parts) > 2 else ''
                    from timing.apex_ws import _GridParser
                    p = _GridParser()
                    p.feed(html)
                    self.stdout.write(self.style.SUCCESS(f'[GRID] {len(p.teams)} rows parsed'))
                    for row in p.teams[:5]:
                        self.stdout.write(f'  → {row}')
                    # Print raw HTML of first 2 rows for debugging
                    import re as _re
                    rows = _re.findall(r'<tr[^>]*>.*?</tr>', html, _re.DOTALL | _re.IGNORECASE)
                    for raw_row in rows[:2]:
                        self.stdout.write(self.style.WARNING(f'  RAW: {raw_row[:300]}'))

                elif msg_type in ('*in', '*out'):
                    kart = parts[1] if len(parts) > 1 else '?'
                    self.stdout.write(self.style.WARNING(f'[PIT {msg_type.upper()}] kart #{kart}'))

                elif msg_type == 'track':
                    track = parts[2] if len(parts) > 2 else '?'
                    self.stdout.write(f'[TRACK] {track}')

                elif msg_type not in ('css', 'dyn1', 'dyn2', 'light', 'best', 'gmt', 'init'):
                    # Show unknown/interesting messages
                    preview = '|'.join(parts)[:80]
                    self.stdout.write(f'[MSG] {preview}')

        if use_ajax:
            self._run_ajax(apex_url, port, duration, on_message, ctx, stop_event)
        else:
            self._run_ws(apex_url, port, duration, on_message, ctx, stop_event)

    def _run_ws(self, apex_url, port, duration, on_message, ctx, stop_event):
        import websocket
        ws_url = f'wss://www.apex-timing.com:{port + 3}/'
        self.stdout.write(f'Connecting WebSocket -> {ws_url}\n')

        def _msg(ws, raw):
            on_message(raw)

        def _open(ws):
            self.stdout.write(self.style.SUCCESS('Connected! Sending init...\n'))
            ws.send('init')

        def _err(ws, err):
            self.stdout.write(self.style.ERROR(f'WS Error: {err}'))

        ws = websocket.WebSocketApp(ws_url, on_open=_open, on_message=_msg, on_error=_err)
        t = threading.Thread(target=lambda: ws.run_forever(sslopt={'cert_reqs': ssl.CERT_NONE}), daemon=True)
        t.start()
        try:
            time.sleep(duration)
        except KeyboardInterrupt:
            pass
        finally:
            ws.close()
            self.stdout.write('\nDone.')

    def _run_ajax(self, apex_url, port, duration, on_message, ctx, stop_event):
        import random, string
        from urllib.parse import urlencode

        AJAX_URL = 'https://live.apex-timing.com/commonv2/functions/live_ajax.php'
        self.stdout.write(f'AJAX polling → {AJAX_URL}\n')

        sess_id = ''.join(random.choices(string.digits, k=8))
        end_at  = time.time() + duration
        counter = 0

        try:
            while time.time() < end_at:
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

                req = Request(AJAX_URL, data=params, headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Referer': apex_url,
                    'Origin': 'https://live.apex-timing.com',
                })
                try:
                    with urlopen(req, context=ctx, timeout=10) as resp:
                        raw = resp.read().decode('utf-8', errors='ignore')
                    for batch in raw.split('@'):
                        on_message(batch)
                    counter += 1
                except Exception as e:
                    self.stdout.write(self.style.ERROR(f'Request error: {e}'))

                time.sleep(2)
        except KeyboardInterrupt:
            pass

        self.stdout.write('\nDone.')
