# epos_web.py — Epson ePOS Emulator + Dashboard Web
# Usage : python epos_web.py [--debug]

import argparse
import json
import logging
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ─── .env loader ───────────────────────────────────────────────────────────────
def _load_env(path: str = '.env'):
    """Charge un fichier .env simple (pas de dépendance externe)."""
    p = Path(path)
    if not p.exists():
        return
    with open(p, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_env()

# ─── Config (toutes les valeurs sont surchargeables par .env ou variable d'env)
EPOS_PORT        = int(os.environ.get('EPOS_PORT',       '80'))
DASHBOARD_PORT   = int(os.environ.get('DASHBOARD_PORT',  '8080'))
TICKET_SAVE_PATH = os.environ.get('TICKET_SAVE_PATH', '').strip()
TICKET_AUTO_SAVE = os.environ.get('TICKET_AUTO_SAVE', 'false').lower() == 'true'
TICKET_FORMAT    = os.environ.get('TICKET_FORMAT',    'xml').lower()   # 'xml' | 'pdf'

# ─── Logging / --debug ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='Epson ePOS Emulator + Dashboard')
parser.add_argument('--debug', action='store_true', help='Logs verbeux')
args, _ = parser.parse_known_args()

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.WARNING,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('epos')

# ─── Sauvegarde tickets ────────────────────────────────────────────────────────
def save_ticket(tid: int, xml_body: str, lines: list):
    """Sauvegarde un ticket en XML brut ou PDF selon TICKET_FORMAT."""
    save_dir = Path(TICKET_SAVE_PATH)
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning(f'Impossible de créer le dossier {save_dir}: {e}')
        return

    ts    = datetime.now().strftime('%Y%m%d_%H%M%S')
    base  = save_dir / f'ticket_{tid:04d}_{ts}'

    if TICKET_FORMAT == 'pdf':
        try:
            from fpdf import FPDF  # pip install fpdf2
            pdf = FPDF()
            pdf.set_margins(8, 10, 8)
            pdf.set_auto_page_break(False)
            pdf.add_page()
            pdf.set_font('Courier', size=9)
            for ln in lines:
                text  = ln.get('text', '')
                align = {'center': 'C', 'right': 'R'}.get(ln.get('align', 'left'), 'L')
                style = 'B' if ln.get('bold') else ''
                pdf.set_font('Courier', style=style, size=9)
                if ln.get('cut'):
                    pdf.cell(0, 4, '-' * 32, align='C', new_x='LMARGIN', new_y='NEXT')
                elif text == '':
                    pdf.ln(3)
                else:
                    pdf.cell(0, 4, text, align=align, new_x='LMARGIN', new_y='NEXT')
            pdf.output(str(base) + '.pdf')
            log.debug(f'Ticket #{tid} → {base}.pdf')
        except ImportError:
            log.warning('fpdf2 non installé → fallback XML. Installer : pip install fpdf2')
            base.with_suffix('.xml').write_text(xml_body, encoding='utf-8')
    else:
        base.with_suffix('.xml').write_text(xml_body, encoding='utf-8')
        log.debug(f'Ticket #{tid} → {base}.xml')

# ─── État partagé ──────────────────────────────────────────────────────────────
events  = deque(maxlen=200)
tickets = deque(maxlen=50)
state   = {
    'total_req':     0,
    'total_tickets': 0,
    'started':       datetime.now().strftime('%d/%m/%Y %H:%M:%S'),
}
lock = threading.Lock()

# ─── Parsing ePOS XML ──────────────────────────────────────────────────────────
# Largeur papier en colonnes monospace (80mm, font A 12 dots/char → 48 cols)
_PAPER_COLS = 48
_CHAR_DOTS  = 12   # largeur d'un caractère standard en dots

def _dots_to_col(x: int) -> int:
    return round(x / _CHAR_DOTS)

def _segments_to_line(segments: list, align: str) -> dict:
    """
    Convertit une liste de segments {x, text, bold} en une ligne rendue.
    Si un seul segment sans x explicite → rendu simple.
    Si plusieurs → colonnes positionnées avec des espaces.
    """
    if not segments:
        return {'text': '', 'align': align, 'bold': False}

    # Tri par position x
    segs = sorted(segments, key=lambda s: s['x'])

    if len(segs) == 1:
        return {'text': segs[0]['text'], 'align': align, 'bold': segs[0]['bold']}

    # Multi-colonnes : construire une chaîne monospace positionnée
    result   = ''
    cur_col  = 0
    any_bold = any(s['bold'] for s in segs)
    for seg in segs:
        col = _dots_to_col(seg['x'])
        if col > cur_col:
            result += ' ' * (col - cur_col)
            cur_col = col
        result  += seg['text']
        cur_col += len(seg['text'])

    return {'text': result, 'align': align, 'bold': any_bold}


def parse_ticket(xml_body: str) -> list:
    """
    Parse un body ePOS XML (avec ou sans envelope SOAP) et retourne
    une liste de lignes : [{text, align, bold, cut?}]
    Retourne [] si rien à imprimer (polling vide).
    """
    try:
        clean = xml_body.strip()
        clean = re.sub(r'\s+xmlns(?::\w+)?="[^"]+"', '', clean)
        clean = re.sub(r'(</?)([\w]+):([\w-]+)', r'\1\3', clean)
        root  = ET.fromstring(clean)

        # Descendre jusqu'à <epos-print>
        target = root
        for elem in root.iter():
            if elem.tag == 'epos-print':
                target = elem
                break

        # ── État de style (cumulatif) ──────────────────────────────────────
        em    = False   # bold (emphase)
        dw    = False   # double-width (→ bold visuel)
        align = 'left'
        cur_x = 0       # position horizontale courante en dots

        # ── Accumulateur de la ligne physique en cours ─────────────────────
        # Un segment = {x, text, bold}  (même ligne tant qu'il n'y a pas de \n)
        cur_segs: list = []

        lines: list = []

        def flush(force_align=None):
            """Vider le segment courant → une ligne."""
            nonlocal cur_x
            la = force_align or align
            if cur_segs:
                lines.append(_segments_to_line(cur_segs, la))
                cur_segs.clear()
            else:
                lines.append({'text': '', 'align': la, 'bold': False})
            cur_x = 0  # reset position horizontale après chaque ligne

        # ── Itération sur les enfants directs de <epos-print> ─────────────
        for elem in target:
            tag = elem.tag

            if tag == 'text':
                # -- Mise à jour de l'état de style --------------------------
                if 'em' in elem.attrib:
                    em = elem.get('em').lower() == 'true'
                if 'dw' in elem.attrib:
                    dw = elem.get('dw').lower() == 'true'
                if 'width' in elem.attrib:
                    try:
                        dw = int(elem.get('width', '1')) >= 2
                    except ValueError:
                        pass
                if 'align' in elem.attrib:
                    align = elem.get('align', align)
                if 'x' in elem.attrib:
                    try:
                        cur_x = int(elem.get('x', '0'))
                    except ValueError:
                        pass

                raw = (elem.text or '').replace('\r', '')
                if not raw:
                    continue  # élément purement stylistique

                bold = em or dw

                # Découpe sur les sauts de ligne explicites (&#10;)
                parts = raw.split('\n')
                for i, part in enumerate(parts):
                    if part:
                        cur_segs.append({'x': cur_x, 'text': part, 'bold': bold})
                        cur_x += len(part) * _CHAR_DOTS
                    # Tout saut de ligne sauf le dernier (qui peut être vide)
                    # déclenche un flush
                    if i < len(parts) - 1:
                        flush()

            elif tag == 'feed':
                if cur_segs:
                    flush()
                n = int(elem.get('line', 1))
                for _ in range(n):
                    lines.append({'text': '', 'align': 'left', 'bold': False})
                cur_x = 0

            elif tag == 'cut':
                if cur_segs:
                    flush()
                lines.append({
                    'text':  '─' * _PAPER_COLS,
                    'align': 'center',
                    'bold':  False,
                    'cut':   True,
                })
                cur_x = 0

        # Flush du dernier segment s'il reste quelque chose
        if cur_segs:
            flush()

        return lines   # vide = rien à imprimer

    except Exception as e:
        log.debug('parse_ticket error: %s', e)
        return []      # on ne pollue pas avec du XML brut


# ─── ePOS Handler  (port 80) ───────────────────────────────────────────────────
_XML_OK = '<?xml version="1.0" encoding="utf-8"?>\n<response success="true" code="0" status="0"/>\n'

class EpsonEPOSHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # silence le log HTTP standard

    def do_GET(self):
        ip = self.client_address[0]
        with lock:
            events.appendleft({
                'ts':     datetime.now().strftime('%H:%M:%S'),
                'method': 'GET',
                'path':   self.path,
                'ip':     ip,
            })
            state['total_req'] += 1
        log.debug('[GET]  %s  ← %s', self.path, ip)
        self._send_xml(_XML_OK)

    def do_POST(self):
        ip     = self.client_address[0]
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length).decode('utf-8', errors='ignore')
        parsed = parse_ticket(body)
        # Ignorer les requêtes sans contenu imprimable (polling / status)
        has_content = any(ln.get('text', '').strip() for ln in parsed)
        with lock:
            state['total_req'] += 1
            event = {
                'ts':     datetime.now().strftime('%H:%M:%S'),
                'method': 'POST',
                'path':   self.path,
                'ip':     ip,
            }
            if has_content:
                state['total_tickets'] += 1
                tid = state['total_tickets']
                ticket = {
                    'id':    tid,
                    'ts':    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'ip':    ip,
                    'path':  self.path,
                    'lines': parsed,
                }
                event['ticket_id'] = tid
                tickets.appendleft(ticket)
            events.appendleft(event)
        if has_content:
            log.debug('[POST] Ticket #%d  ← %s  (%d lignes)', tid, ip, len(parsed))
            if TICKET_AUTO_SAVE and TICKET_SAVE_PATH:
                save_ticket(tid, body, parsed)
        else:
            log.debug('[POST] Polling/status  ← %s', ip)
        self._send_xml(_XML_OK)

    def _send_xml(self, xml):
        body = xml.encode()
        self.send_response(200)
        self.send_header('Content-Type',   'text/xml; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)


# ─── Dashboard Handler  (port 6666) ────────────────────────────────────────────
class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        routes = {
            '/':            self._serve_html,
            '/api/events':  self._serve_events,
            '/api/tickets': self._serve_tickets,
            '/api/stats':   self._serve_stats,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        body = DASHBOARD_HTML.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type',   'text/html; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type',              'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length',            len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self):
        with lock:
            self._json_response(list(events)[:30])

    def _serve_tickets(self):
        with lock:
            self._json_response(list(tickets)[:10])

    def _serve_stats(self):
        with lock:
            self._json_response(dict(state))


# ─── Dashboard HTML / CSS / JS ─────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ePOS Dashboard</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --card:    #21262d;
    --border:  #30363d;
    --text:    #e6edf3;
    --muted:   #8b949e;
    --green:   #3fb950;
    --blue:    #58a6ff;
    --orange:  #d29922;
    --sel:     rgba(88,166,255,.1);
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  header h1 { font-size: 16px; font-weight: 600; }
  .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%,100% { box-shadow: 0 0 4px var(--green); }
    50%      { box-shadow: 0 0 12px var(--green); }
  }
  .hright { margin-left: auto; color: var(--muted); font-size: 12px; }
  .hright b { color: var(--text); font-weight: normal; }

  .sbar {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .sc { background: var(--surface); padding: 10px 16px; }
  .sc .lbl { font-size: 10px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
  .sc .val { font-size: 22px; font-weight: 700; }

  .main {
    flex: 1;
    display: flex;
    overflow: hidden;
  }

  /* journal */
  .jpanel {
    display: flex;
    flex-direction: column;
    border-right: none;
    overflow: hidden;
    min-width: 200px;
    flex: 1 1 0;
  }
  .phdr {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 8px 14px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .phdr .cnt {
    margin-left: auto;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 1px 8px;
    font-size: 11px;
    color: var(--text);
  }
  .pbody { flex: 1; overflow-y: auto; }
  .pbody::-webkit-scrollbar { width: 5px; }
  .pbody::-webkit-scrollbar-track { background: transparent; }
  .pbody::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .row {
    display: grid;
    grid-template-columns: 60px 46px 1fr 108px 90px;
    align-items: center;
    gap: 8px;
    padding: 7px 14px;
    border-bottom: 1px solid rgba(48,54,61,.5);
    font-size: 12px;
  }
  .row.tkt { cursor: pointer; }
  .row.tkt:hover { background: var(--card); }
  .row.sel { background: var(--sel) !important; border-left: 2px solid var(--blue); padding-left: 12px; }
  .row.flash { animation: rf .6s ease-out; }
  @keyframes rf { 0% { background: rgba(63,185,80,.2); } 100% { background: transparent; } }
  .ts  { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .bdg { font-size: 10px; font-weight: 700; padding: 2px 5px; border-radius: 3px; text-align: center; }
  .bG  { background: #1f3a5f; color: var(--blue); }
  .bP  { background: #1a3a26; color: var(--green); }
  .path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ip  { color: var(--muted); font-size: 11px; font-family: monospace; text-align: right; }
  .ico { text-align: right; }
  .pbtn {
    font-size: 10px; font-weight: 600;
    padding: 2px 8px; border-radius: 3px;
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted);
    cursor: pointer;
    white-space: nowrap;
    transition: background .15s, color .15s, border-color .15s;
  }
  .pbtn:hover { background: var(--blue); color: #fff; border-color: var(--blue); }

  /* ── Resize handle ── */
  .resize-handle {
    width: 5px;
    cursor: col-resize;
    background: var(--border);
    flex-shrink: 0;
    transition: background .15s;
    position: relative;
    z-index: 10;
  }
  .resize-handle:hover, .resize-handle.dragging { background: var(--blue); }

  /* viewer */
  .vpanel {
    display: flex;
    flex-direction: column;
    background: var(--bg);
    overflow: hidden;
    min-width: 200px;
    flex: 0 0 400px;
  }
  .vhdr {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 8px 14px;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    flex-shrink: 0;
  }
  .tgl {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 7px;
    cursor: pointer;
    user-select: none;
    text-transform: none;
    letter-spacing: 0;
    font-weight: 400;
    color: var(--muted);
    font-size: 11px;
  }
  .tgl input { display: none; }
  .ttrack {
    width: 30px; height: 17px;
    background: var(--border);
    border-radius: 9px;
    position: relative;
    transition: background .2s;
    flex-shrink: 0;
  }
  .ttrack::after {
    content: '';
    position: absolute;
    top: 3px; left: 3px;
    width: 11px; height: 11px;
    border-radius: 50%;
    background: var(--muted);
    transition: left .2s, background .2s;
  }
  .tgl input:checked ~ .ttrack { background: var(--green); }
  .tgl input:checked ~ .ttrack::after { left: 16px; background: #fff; }
  .tlbl { transition: color .2s; }
  .tgl input:checked ~ .tlbl { color: var(--green); }
  .vbody {
    flex: 1;
    overflow-y: auto;
    padding: 20px 14px;
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .vbody::-webkit-scrollbar { width: 5px; }
  .vbody::-webkit-scrollbar-track { background: transparent; }
  .vbody::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  .empty {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; height: 100%; gap: 10px;
    color: var(--muted); font-size: 13px;
  }
  .empty .eico { font-size: 36px; opacity: .2; }

  /* receipt paper */
  .vbody { align-items: stretch; }
  .rwrap {
    width: 100%;
    filter: drop-shadow(0 4px 20px rgba(0,0,0,.7));
  }
  .rtop {
    height: 12px;
    background: radial-gradient(circle at 6px 12px, var(--bg) 6px, #f5f5f5 6px);
    background-size: 12px 12px;
    background-repeat: repeat-x;
  }
  .rcard {
    background: #f5f5f5;
    color: #111;
    font-family: 'Courier New', Courier, monospace;
    font-size: 12px;
    line-height: 1.55;
    overflow-x: auto;
  }
  .rmeta {
    background: #1a1a1a;
    color: #ccc;
    padding: 6px 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11px;
    font-family: 'Segoe UI', sans-serif;
    position: sticky;
    top: 0;
  }
  .rmeta .rip { color: #3fb950; font-weight: 600; }
  .rmeta .rts { color: #888; }
  .rbody { padding: 8px 12px 10px; }
  /* une ligne = un <span> block, white-space:pre pour conserver l'espacement ePOS */
  .rl    { display: block; white-space: pre; line-height: 1.55; min-height: 1.55em; }
  .rl.c  { text-align: center; }
  .rl.r  { text-align: right; }
  .rl.b  { font-weight: 700; }
  .rl.x  { color: #999; text-align: center; }
  .rbot {
    height: 12px;
    background: radial-gradient(circle at 6px 0px, var(--bg) 6px, #f5f5f5 6px);
    background-size: 12px 12px;
    background-repeat: repeat-x;
  }
</style>
</head>
<body>

<header>
  <span style="font-size:20px">🖨️</span>
  <h1>ePOS Emulator</h1>
  <div class="dot"></div>
  <div class="hright">Démarré le <b id="started">–</b> &nbsp;|&nbsp; ePOS sur port <b>80</b></div>
</header>

<div class="sbar">
  <div class="sc"><div class="lbl">Requêtes</div><div class="val" style="color:var(--blue)" id="s-req">0</div></div>
  <div class="sc"><div class="lbl">Tickets</div><div class="val" style="color:var(--green)" id="s-tkt">0</div></div>
  <div class="sc"><div class="lbl">Dashboard</div><div class="val" style="color:var(--orange)">:8080</div></div>
  <div class="sc"><div class="lbl">ePOS</div><div class="val" style="color:var(--blue)">:80</div></div>
</div>

<div class="main">

  <!-- Journal -->
  <div class="jpanel" id="jpanel">
    <div class="phdr">📡 Journal des connexions <span class="cnt" id="ev-cnt">0</span></div>
    <div class="pbody" id="jbody">
      <div class="empty"><span class="eico">📡</span>En attente de connexions…</div>
    </div>
  </div>

  <!-- Resize handle -->
  <div class="resize-handle" id="rhandle"></div>

  <!-- Viewer -->
  <div class="vpanel" id="vpanel">
    <div class="vhdr">
      🧾 Ticket reçu
      <label class="tgl" title="Afficher automatiquement le dernier ticket reçu">
        <input type="checkbox" id="auto-last" checked>
        <span class="ttrack"></span>
        <span class="tlbl">Dernier auto</span>
      </label>
    </div>
    <div class="vbody" id="vbody">
      <div class="empty"><span class="eico">🧾</span>Aucun ticket sélectionné</div>
    </div>
  </div>

</div>

<script>
  let selId    = null;
  let autoLast = true;
  let tMap     = {};
  let prevEv   = 0;
  let lastEvs  = [];

  const autoToggle = document.getElementById('auto-last');

  autoToggle.addEventListener('change', () => {
    autoLast = autoToggle.checked;
    if (autoLast) {
      const ids = Object.keys(tMap).map(Number).sort((a,b) => b-a);
      if (ids.length) { selId = ids[0]; renderJournal(lastEvs); renderViewer(); }
    }
  });

  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function selectTicket(id) {
    selId = id;
    autoLast = false;
    autoToggle.checked = false;
    renderJournal(lastEvs);
    renderViewer();
  }

  function printTicket(id) {
    const t = tMap[id];
    if (!t) return;
    const lines = t.lines.map(ln => {
      let style = 'display:block;white-space:pre-wrap;word-break:break-word;min-height:1.6em;font-family:"Courier New",monospace;font-size:13px;';
      if (ln.align === 'center') style += 'text-align:center;';
      else if (ln.align === 'right') style += 'text-align:right;';
      if (ln.bold) style += 'font-weight:700;';
      if (ln.cut)  style += 'color:#999;text-align:center;';
      return `<span style="${style}">${ln.text === '' ? '\u00a0' : esc(ln.text)}</span>`;
    }).join('');
    const w = window.open('','_blank','width=400,height=600');
    w.document.write(`<!DOCTYPE html><html><head><title>Ticket #${id}</title>
    <style>
      body{margin:0;background:#fff;color:#111;font-family:'Courier New',monospace;}
      .wrap{max-width:340px;margin:0 auto;padding:12px 16px;}
      .meta{font-family:'Segoe UI',sans-serif;font-size:11px;color:#888;margin-bottom:8px;display:flex;justify-content:space-between;}
      .meta b{color:#111;}
      @media print{.noprint{display:none}}
    </style></head><body>
    <div class="wrap">
      <div class="meta noprint"><b>${esc(t.ip)}</b><span>${esc(t.ts)}</span></div>
      ${lines}
    </div>
    <script>window.onload=()=>{window.print();}<\/script>
    </body></html>`);
    w.document.close();
  }

  function renderJournal(events) {
    lastEvs = events;
    const body = document.getElementById('jbody');
    if (!events.length) {
      body.innerHTML = '<div class="empty"><span class="eico">📡</span>En attente…</div>';
      document.getElementById('ev-cnt').textContent = '0';
      return;
    }
    const isNew = events.length > prevEv;
    body.innerHTML = events.map((e, i) => {
      const hasTkt = !!e.ticket_id;
      let cls = 'row' + (hasTkt ? ' tkt' : '') +
                (hasTkt && e.ticket_id === selId ? ' sel' : '') +
                (isNew && i === 0 ? ' flash' : '');
      const click = hasTkt ? `onclick="selectTicket(${e.ticket_id})"` : '';
      const bdg = e.method === 'POST'
        ? '<span class="bdg bP">POST</span>'
        : '<span class="bdg bG">GET</span>';
      const printBtn = hasTkt
        ? `<button class="pbtn" onclick="event.stopPropagation();printTicket(${e.ticket_id})">🖨 Print</button>`
        : '';
      return `<div class="${cls}" ${click}>
        <span class="ts">${esc(e.ts)}</span>
        ${bdg}
        <span class="path" title="${esc(e.path)}">${esc(e.path)}</span>
        <span class="ip">${esc(e.ip)}</span>
        <span class="ico">${printBtn}</span>
      </div>`;
    }).join('');
    prevEv = events.length;
    document.getElementById('ev-cnt').textContent = events.length;
  }

  function renderViewer() {
    const body = document.getElementById('vbody');
    if (!selId || !tMap[selId]) {
      body.innerHTML = '<div class="empty"><span class="eico">🧾</span>Aucun ticket sélectionné</div>';
      return;
    }
    const t = tMap[selId];
    const lines = t.lines.map(ln => {
      let cls = 'rl';
      if (ln.align === 'center') cls += ' c';
      else if (ln.align === 'right') cls += ' r';
      if (ln.bold) cls += ' b';
      if (ln.cut)  cls += ' x';
      return `<span class="${cls}">${ln.text === '' ? '\u00a0' : esc(ln.text)}</span>`;
    }).join('');
    body.innerHTML = `
      <div class="rwrap">
        <div class="rtop"></div>
        <div class="rcard">
          <div class="rmeta">
            <span class="rip">${esc(t.ip)}</span>
            <span class="rts">${esc(t.ts)}</span>
          </div>
          <div class="rbody">${lines}</div>
        </div>
        <div class="rbot"></div>
      </div>`;
  }

  async function refresh() {
    try {
      const [stats, events, tickets] = await Promise.all([
        fetch('/api/stats').then(r => r.json()),
        fetch('/api/events').then(r => r.json()),
        fetch('/api/tickets').then(r => r.json()),
      ]);
      document.getElementById('s-req').textContent   = stats.total_req;
      document.getElementById('s-tkt').textContent   = stats.total_tickets;
      document.getElementById('started').textContent = stats.started;
      const nm = {};
      tickets.forEach(t => { nm[t.id] = t; });
      tMap = nm;
      if (autoLast && tickets.length > 0) selId = tickets[0].id;
      renderJournal(events);
      renderViewer();
    } catch(e) {}
  }

  // ── Resize handle ──────────────────────────────────────────────────────────
  (function() {
    const handle = document.getElementById('rhandle');
    const jpanel = document.getElementById('jpanel');
    const vpanel = document.getElementById('vpanel');
    let dragging = false, startX = 0, startJ = 0, startV = 0;

    handle.addEventListener('mousedown', e => {
      dragging = true;
      startX   = e.clientX;
      startJ   = jpanel.getBoundingClientRect().width;
      startV   = vpanel.getBoundingClientRect().width;
      handle.classList.add('dragging');
      document.body.style.cursor    = 'col-resize';
      document.body.style.userSelect = 'none';
    });

    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      const dx   = e.clientX - startX;
      const newJ = Math.max(160, startJ + dx);
      const newV = Math.max(160, startV - dx);
      jpanel.style.flex = `0 0 ${newJ}px`;
      vpanel.style.flex = `0 0 ${newV}px`;
    });

    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      handle.classList.remove('dragging');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
    });
  })();

  refresh();
  setInterval(refresh, 2000);
</script>
</body>
</html>
"""

# ─── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    epos_server = HTTPServer(('0.0.0.0', EPOS_PORT), EpsonEPOSHandler)
    t = threading.Thread(target=epos_server.serve_forever, daemon=True)
    t.start()

    print('=' * 60)
    print('  ePOS Emulator + Dashboard')
    print('=' * 60)
    print(f'  [ePOS]      port {EPOS_PORT}  ← Symbioz/caisse')
    print(f'  [Dashboard] http://0.0.0.0:{DASHBOARD_PORT}')
    if TICKET_AUTO_SAVE and TICKET_SAVE_PATH:
        print(f'  [Save]      {TICKET_SAVE_PATH}  (format={TICKET_FORMAT})')
    if args.debug:
        print('  [Mode]      DEBUG activé')
    print('=' * 60)

    dashboard_server = HTTPServer(('0.0.0.0', DASHBOARD_PORT), DashboardHandler)
    dashboard_server.serve_forever()
