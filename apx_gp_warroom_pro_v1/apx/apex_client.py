from __future__ import annotations

import json
import re
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from .models import TeamTiming
from .timeutils import parse_lap_time


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_cell: List[str] = []
        self.current_row: List[str] = []
        self.tables: List[List[List[str]]] = []
        self.current_table: List[List[str]] = []
        self.text_chunks: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.in_table = True
            self.current_table = []
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.current_row = []
        elif self.in_table and tag in {"td", "th"}:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.in_cell:
            txt = " ".join("".join(self.current_cell).split())
            self.current_row.append(txt)
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.current_table.append(self.current_row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            if self.current_table:
                self.tables.append(self.current_table)
            self.in_table = False

    def handle_data(self, data):
        text = data.strip()
        if text:
            self.text_chunks.append(text)
        if self.in_cell:
            self.current_cell.append(data)


class ApexClient:
    """Best-effort Apex Timing reader.

    Important: Apex pages can be dynamic. This client is intentionally conservative:
    1) fetch page HTML;
    2) parse HTML tables if present;
    3) look for JSON payloads in scripts;
    4) otherwise return [] and keep the dashboard alive.

    The Portuguese programmer should inspect the real Alcanede page with DevTools -> Network
    and replace/add the exact endpoint if one exists.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.url = config.get("event_url", "").strip()
        self.timeout = 8
        self.user_agent = "APX-GP-WarRoom/1.0 (+local team dashboard)"

    def fetch(self) -> List[TeamTiming]:
        if not self.url:
            return []
        html = self._fetch_html(self.url)
        rows = self._parse_html_tables(html)
        if rows:
            return rows
        rows = self._parse_embedded_json(html)
        return rows

    def _fetch_html(self, url: str) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    def _parse_html_tables(self, html: str) -> List[TeamTiming]:
        parser = _TableParser()
        parser.feed(html)
        candidates: List[TeamTiming] = []
        for table in parser.tables:
            if len(table) < 2:
                continue
            headers = [h.lower().strip() for h in table[0]]
            if not _looks_like_timing_table(headers):
                continue
            for row in table[1:]:
                item = _row_to_timing(headers, row)
                if item and item.name:
                    candidates.append(item)
        return candidates

    def _parse_embedded_json(self, html: str) -> List[TeamTiming]:
        # Generic rescue parser. Real Apex may not expose usable JSON in the HTML.
        # We try to identify arrays of dicts with timing-like keys.
        rows: List[TeamTiming] = []
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S | re.I)
        for script in scripts:
            for blob in re.findall(r"(\[[\s\S]{20,50000}?\])", script):
                try:
                    data = json.loads(blob)
                except Exception:
                    continue
                if isinstance(data, list):
                    for obj in data:
                        if isinstance(obj, dict):
                            item = _dict_to_timing(obj)
                            if item:
                                rows.append(item)
                    if rows:
                        return rows
        return rows


def _looks_like_timing_table(headers: List[str]) -> bool:
    joined = " ".join(headers)
    must_have = any(k in joined for k in ["driver", "team", "name", "piloto", "equipa"])
    timing = any(k in joined for k in ["lap", "best", "laps", "pits", "gap", "interval", "delta", "time"])
    return must_have and timing


def _row_to_timing(headers: List[str], row: List[str]) -> Optional[TeamTiming]:
    data = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
    return _dict_to_timing(data)


def _dict_to_timing(data: Dict[str, Any]) -> Optional[TeamTiming]:
    def pick(*names: str) -> Any:
        lower = {str(k).lower(): v for k, v in data.items()}
        for n in names:
            if n in lower and str(lower[n]).strip():
                return lower[n]
        # soft contains match
        for key, value in lower.items():
            for n in names:
                if n in key and str(value).strip():
                    return value
        return None

    name = pick("team", "driver", "name", "equipa", "piloto")
    if not name:
        return None
    return TeamTiming(
        position=_int_or_none(pick("pos", "position", "rank", "p")),
        name=str(name).strip(),
        last_lap=parse_lap_time(pick("last", "last lap", "lap", "time")),
        best_lap=parse_lap_time(pick("best", "best lap")),
        laps=_int_or_none(pick("laps", "voltas")),
        pits=_int_or_none(pick("pits", "pit", "paragens")),
        gap=str(pick("gap") or ""),
        interval=str(pick("interval", "int") or ""),
        delta=str(pick("delta") or ""),
        on_track=str(pick("on track", "pit time", "track") or ""),
        raw=dict(data),
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).replace(",", ".").replace("+", "")))
    except Exception:
        return None
