# Epson ePOS Emulator

A lightweight Epson ePOS print server emulator written in pure Python (stdlib only).  
Designed to receive print jobs from **Symbioz** (or any ePOS-compatible POS system) and display them in a real-time **web dashboard**.

---

## Features

- **ePOS emulator** — responds `success="true"` to all print requests (port 80 by default)
- **Web dashboard** — live connection log + receipt viewer (port 8080 by default)
- **Receipt rendering** — parses ePOS XML (SOAP wrapper, namespaces, bold, alignment, cut)
- **Auto-display** — toggle to always show the latest ticket automatically
- **Print button** — open any ticket in a printable browser window
- **Resizable panels** — drag the divider between journal and viewer
- **Auto-save** — optionally save each print job as raw XML or PDF
- **Zero dependencies** — runs with Python 3.8+ stdlib; `fpdf2` only needed for PDF export
- **Docker ready** — includes `Dockerfile` and `docker-compose.yml`
- **`--debug` flag** — silent by default, verbose logs on demand

---

## Quick Start

### Without Docker

```bash
# Clone
git clone https://github.com/BuggedToast/epson-epos-emulator.git
cd epson-epos-emulator

# Copy config (optional)
cp .env.example .env

# Run (port 80 requires sudo/admin)
sudo python epos_web.py
# or on Windows (run as Administrator)
python epos_web.py
```

Open **http://localhost:8080** in your browser.

### With Docker

```bash
git clone https://github.com/BuggedToast/epson-epos-emulator.git
cd epson-epos-emulator

cp .env.example .env   # edit values if needed
docker compose up -d
```

---

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `EPOS_PORT` | `80` | Port for the ePOS emulator (Symbioz points here) |
| `DASHBOARD_PORT` | `8080` | Port for the web dashboard |
| `TICKET_AUTO_SAVE` | `false` | Save every print job automatically |
| `TICKET_SAVE_PATH` | *(empty)* | Folder where tickets are saved |
| `TICKET_FORMAT` | `xml` | Save format: `xml` (raw) or `pdf` (requires `fpdf2`) |

All variables can also be passed as environment variables (e.g. in `docker-compose.yml`).

---

## Debug mode

```bash
python epos_web.py --debug
```

Prints every GET/POST request and ticket content to stdout.  
By default the console is **silent** (only the startup banner is shown).

---

## PDF export

```bash
pip install fpdf2
```

Then set in `.env`:
```
TICKET_AUTO_SAVE=true
TICKET_SAVE_PATH=/tmp/tickets
TICKET_FORMAT=pdf
```

---

## Auto-start on Ubuntu (systemd)

```bash
# Copy files to the server
scp epos_web.py .env user@192.168.1.245:/opt/epos/

# On the server
sudo tee /etc/systemd/system/epos.service > /dev/null << EOF
[Unit]
Description=Epson ePOS Emulator + Dashboard
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/epos/epos_web.py
WorkingDirectory=/opt/epos
Restart=always
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now epos
sudo journalctl -u epos -f   # follow logs
```

---

## Dashboard

| Panel | Description |
|---|---|
| **Connection log** (left) | Every GET/POST with timestamp, method badge, path and IP. Rows with a print job show a 🖨 Print button and are clickable. |
| **Ticket viewer** (right) | Renders the selected ticket as a thermal receipt. Toggle **"Dernier auto"** to always display the latest one. |

The divider between panels is **draggable**.

---

## License

MIT
