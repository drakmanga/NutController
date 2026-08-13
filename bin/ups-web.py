#!/usr/bin/env python3
from flask import Flask, jsonify, request, Response
from datetime import datetime, timedelta
import subprocess, re, threading, time, json, os, requests

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = '/etc/nut/nutcontroller.conf'
config_lock = threading.Lock()

def load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    cfg[k.strip()] = v.strip().strip('"\'')
    except Exception:
        pass
    return cfg

def save_config_values(values):
    """Aggiorna (o aggiunge) le chiavi indicate in nutcontroller.conf preservando il resto del file."""
    with config_lock:
        try:
            with open(CONFIG_PATH) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        remaining = dict(values)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith('#') and '=' in stripped:
                key = stripped.split('=', 1)[0].strip()
                if key in remaining:
                    lines[i] = f'{key}="{remaining.pop(key)}"\n'
        for key, val in remaining.items():
            lines.append(f'{key}="{val}"\n')
        with open(CONFIG_PATH, 'w') as f:
            f.writelines(lines)

CFG = load_config()

BOT_TOKEN          = CFG.get('BOT_TOKEN', '')
CHAT_ID            = CFG.get('CHAT_ID', '')
WEB_URL            = CFG.get('WEB_URL', 'http://192.168.0.111')
LOAD_WATTS            = float(CFG.get('LOAD_WATTS', 44))
BATTERY_RUNTIME_FULL  = float(CFG.get('BATTERY_RUNTIME_FULL', 2400))

LOG_FILE     = "/var/lib/nut/blackout.log"
METRICS_FILE = "/var/lib/nut/metrics.jsonl"

metrics = []

# ── UPS helpers ──────────────────────────────────────────────────────────────

def get_ups_data():
    try:
        r = subprocess.run(['upsc', 'myups'], capture_output=True, text=True, timeout=5)
        stats = {}
        for line in r.stdout.strip().split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
                stats[k.strip()] = v.strip()
        return stats
    except Exception:
        return {}

def format_autonomy(runtime_sec):
    """Autonomia basata sul battery.runtime reale riportato dall'UPS (non calcolata da Ah/W)."""
    try:
        s = int(float(runtime_sec))
        if s <= 0:
            return "N/A"
        h, m = divmod(s // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "N/A"

def autonomy_pct(runtime_sec):
    try:
        return round(min(100, max(0, float(runtime_sec) / BATTERY_RUNTIME_FULL * 100)), 1)
    except Exception:
        return 0

def get_uptime():
    try:
        secs = int(float(open('/proc/uptime').read().split()[0]))
        d, r = divmod(secs, 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        return f"{d}g {h}h {m}m" if d else f"{h}h {m}m"
    except Exception:
        return "N/A"

UPS_CONF_PATH = '/etc/nut/ups.conf'
UPS_NAME      = 'myups'  # nome della stanza NUT usata da questa dashboard
ups_conf_lock = threading.Lock()

def read_ups_stanza(name=UPS_NAME):
    """Legge le direttive (driver/port/desc/...) dentro [name] in ups.conf."""
    values = {}
    in_section = False
    try:
        with open(UPS_CONF_PATH) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('['):
                    in_section = (stripped == f'[{name}]')
                    continue
                if in_section and stripped and not stripped.startswith('#') and '=' in stripped:
                    k, v = stripped.split('=', 1)
                    values[k.strip()] = v.strip().strip('"\'')
    except FileNotFoundError:
        pass
    return values

def save_ups_stanza(values, name=UPS_NAME):
    """Aggiorna le direttive indicate dentro [name] in ups.conf, preservando il resto del file."""
    with ups_conf_lock:
        try:
            with open(UPS_CONF_PATH) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        remaining      = dict(values)
        in_section     = False
        section_found  = False
        insert_at      = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('['):
                if in_section and remaining and insert_at is None:
                    insert_at = i
                in_section = (stripped == f'[{name}]')
                section_found = section_found or in_section
                continue
            if in_section and stripped and not stripped.startswith('#') and '=' in stripped:
                key = stripped.split('=', 1)[0].strip()
                if key in remaining:
                    indent = line[:len(line) - len(line.lstrip())]
                    lines[i] = f'{indent}{key} = "{remaining.pop(key)}"\n'
        if in_section and remaining and insert_at is None:
            insert_at = len(lines)
        if remaining:
            new_lines = [f'\t{k} = "{v}"\n' for k, v in remaining.items()]
            if section_found:
                idx = insert_at if insert_at is not None else len(lines)
                lines[idx:idx] = new_lines
            else:
                if lines and not lines[-1].endswith('\n'):
                    lines[-1] += '\n'
                lines.append(f'\n[{name}]\n')
                lines.extend(new_lines)
        with open(UPS_CONF_PATH, 'w') as f:
            f.writelines(lines)

def parse_duration_sec(s):
    if not s:
        return None
    m = re.match(r'^(\d+)\s*minut', s)
    if m:
        return int(m.group(1)) * 60
    secs = 0
    for val, unit in re.findall(r'(\d+)([hms])', s):
        if unit == 'h':   secs += int(val) * 3600
        elif unit == 'm': secs += int(val) * 60
        elif unit == 's': secs += int(val)
    return secs

def sec_to_str(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, s2 = divmod(r, 60)
    if h:  return f"{h}h {m}m {s2}s"
    if m:  return f"{m}m {s2}s"
    return f"{s2}s"

ORPHAN_AFTER_SEC = 6 * 3600  # oltre questa soglia senza FINE, un evento non e' piu' "in corso" ma orfano (fine mai registrata)

def read_history():
    try:
        lines = [l.strip() for l in open(LOG_FILE) if l.strip()]
        events, current = [], None
        for line in lines:
            if "INIZIO blackout" in line:
                if current is not None:
                    # Nuovo INIZIO senza una FINE precedente (es. reboot del CT
                    # a meta' blackout non ancora gestito): non perdere l'evento
                    # aperto, registralo com'e' invece di sovrascriverlo.
                    events.append(current)
                current = {"start": line.split(" | ")[0], "end": None, "duration": None}
            elif "FINE blackout" in line and current:
                current["end"] = line.split(" | ")[0]
                m = re.search(r'durata: (.+)', line)
                current["duration"] = m.group(1) if m else "N/A"
                events.append(current)
                current = None
        if current:
            events.append(current)
        events = list(reversed(events))
        now = datetime.now()
        for e in events:
            if e["end"] is None:
                try:
                    start_dt = datetime.strptime(e["start"], "%Y-%m-%d %H:%M:%S")
                    e["orphaned"] = (now - start_dt).total_seconds() > ORPHAN_AFTER_SEC
                except Exception:
                    e["orphaned"] = False
        return events
    except FileNotFoundError:
        return []

def get_blackout_stats(history):
    if not history:
        return {"total": 0, "avg": None, "max": None, "last": None}
    completed  = [e for e in history if e.get("duration") and e.get("end")]
    durations  = [(parse_duration_sec(e["duration"]), e) for e in completed]
    durations  = [(d, e) for d, e in durations if d is not None]
    avg = sec_to_str(sum(d for d, _ in durations) / len(durations)) if durations else None
    worst_sec, worst_event = max(durations, key=lambda x: x[0]) if durations else (None, None)
    return {
        "total":      len(history),
        "avg":        avg,
        "max":        sec_to_str(worst_sec) if worst_sec is not None else None,
        "max_date":   worst_event["start"].split(" ")[0] if worst_event else None,
        "max_start":  worst_event["start"] if worst_event else None,
        "last":       history[0].get("start") if history else None,
    }

# ── Metrics logger ───────────────────────────────────────────────────────────

def load_metrics():
    try:
        with open(METRICS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        metrics.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass

def save_metric(pt):
    with open(METRICS_FILE, 'a') as f:
        f.write(json.dumps(pt) + '\n')

def metrics_loop():
    while True:
        try:
            d = get_ups_data()
            if d:
                nominal  = float(d.get('ups.realpower.nominal') or 0)
                load_pct = float(d.get('ups.load') or 0)
                watts    = round(nominal * load_pct / 100, 1) if nominal else 0
                pt = {
                    'ts':     datetime.now().isoformat(timespec='seconds'),
                    'charge': float(d.get('battery.charge') or 0),
                    'load':   load_pct,
                    'watts':  watts,
                }
                metrics.append(pt)
                save_metric(pt)
        except Exception:
            pass
        time.sleep(60)

load_metrics()
threading.Thread(target=metrics_loop, daemon=True).start()

# ── Telegram bot ──────────────────────────────────────────────────────────────

def format_bot_stats():
    d = get_ups_data()
    raw = d.get('ups.status', 'N/A')
    label, _ = STATUS_MAP.get(raw, (raw, 'unknown'))
    charge     = d.get('battery.charge', 'N/A')
    load_pct   = d.get('ups.load', '0')
    nominal    = float(d.get('ups.realpower.nominal') or 0)
    actual_w   = round(nominal * float(load_pct or 0) / 100, 1) if nominal else LOAD_WATTS
    autonomy   = format_autonomy(d.get('battery.runtime'))
    status_icons = {'OL': '✅', 'OB': '⚠️', 'LB': '🔴', 'OL CHRG': '🔌'}
    icon = status_icons.get(raw, 'ℹ️')
    return (
        f"📊 *Statistiche UPS*\n\n"
        f"{icon} *Stato:* {label}\n"
        f"🔋 *Carica:* {charge}%\n"
        f"⏱ *Autonomia:* {autonomy}\n"
        f"⚡ *Consumo:* {actual_w}W"
    )

def format_bot_history():
    history = read_history()
    if not history:
        return "📋 Nessun blackout registrato."
    lines = ["📋 *Storico blackout:*\n"]
    for i, e in enumerate(history[:20]):
        start = e.get('start') or '—'
        date, _, start_t = start.partition(' ')
        end = e.get('end')
        if end:
            end_t = end.split(' ')[1]
        elif e.get('orphaned'):
            end_t = 'fine non presente'
        else:
            end_t = 'in corso'
        dur = e.get('duration') or '—'
        lines.append(f"{len(history)-i}. {date} · {start_t}–{end_t} ({dur})")
    return '\n'.join(lines)

bot_state = {'bot': None, 'thread': None}
bot_state_lock = threading.Lock()

def _bot_run(token, chat_id):
    try:
        import telebot
        bot = telebot.TeleBot(token, parse_mode='Markdown')
        bot_state['bot'] = bot

        def guard(message):
            return str(message.chat.id) == str(chat_id)

        @bot.message_handler(commands=['start', 'help'])
        def cmd_help(message):
            if not guard(message): return
            bot.reply_to(message,
                "🤖 *UPS Bot*\n\n"
                "/stats — Statistiche UPS\n"
                "/history — Storico blackout\n"
                f"\n🖥 {WEB_URL}")

        @bot.message_handler(commands=['stats'])
        def cmd_stats(message):
            if not guard(message): return
            bot.reply_to(message, format_bot_stats())

        @bot.message_handler(commands=['history'])
        def cmd_history(message):
            if not guard(message): return
            bot.reply_to(message, format_bot_history())

        bot.infinity_polling(timeout=30, long_polling_timeout=20)
    except Exception:
        pass
    finally:
        if bot_state.get('bot') is not None:
            bot_state['bot'] = None

def stop_bot():
    """Ferma il polling del bot Telegram attivo, se presente. Da chiamare tenendo bot_state_lock."""
    b = bot_state.get('bot')
    if b is not None:
        try:
            b.stop_polling()
        except Exception:
            pass
    th = bot_state.get('thread')
    if th is not None and th.is_alive():
        th.join(timeout=5)
    bot_state['bot'] = None
    bot_state['thread'] = None

def start_bot(token, chat_id):
    """(Ri)avvia il bot Telegram con le credenziali indicate, fermando quello precedente."""
    with bot_state_lock:
        stop_bot()
        if not token or not chat_id:
            return
        t = threading.Thread(target=_bot_run, args=(token, chat_id), daemon=True)
        bot_state['thread'] = t
        t.start()

if BOT_TOKEN and CHAT_ID:
    start_bot(BOT_TOKEN, CHAT_ID)

# ── Collegamento guidato Telegram ───────────────────────────────────────────
# Flusso: l'utente incolla il token del bot (da @BotFather) e preme "Collega".
# Il server valida il token, mette in pausa il bot corrente se necessario e si
# mette in ascolto (getUpdates) finche' non arriva un messaggio qualsiasi: da
# li' ricava il chat_id automaticamente, senza che l'utente debba cercarlo a mano.

LINK_TIMEOUT_SEC = 120
TELEGRAM_API = 'https://api.telegram.org/bot{token}/{method}'

link_state = {
    'active':    False,   # in ascolto di un messaggio
    'token':     None,
    'status':    'idle',  # idle | listening | found | timeout | error
    'chat_id':   None,
    'chat_name': None,
    'bot_username': None,
    'error':     None,
    'paused_main_bot': False,  # True se abbiamo dovuto fermare il bot attivo per testare lo stesso token
}
link_lock = threading.Lock()

def tg_get_me(token):
    r = requests.get(TELEGRAM_API.format(token=token, method='getMe'), timeout=10)
    data = r.json()
    if not data.get('ok'):
        raise ValueError(data.get('description', 'token non valido'))
    return data['result']

def _link_listen(token):
    try:
        # Scarta gli update gia' presenti in coda, cosi' non "rileviamo" un vecchio messaggio.
        r = requests.get(TELEGRAM_API.format(token=token, method='getUpdates'),
                          params={'timeout': 0}, timeout=10).json()
        offset = None
        if r.get('ok') and r.get('result'):
            offset = r['result'][-1]['update_id'] + 1

        deadline = time.time() + LINK_TIMEOUT_SEC
        while time.time() < deadline:
            with link_lock:
                if not link_state['active'] or link_state['token'] != token:
                    return  # annullato dall'utente o sostituito da un nuovo tentativo
            params = {'timeout': 20}
            if offset is not None:
                params['offset'] = offset
            resp = requests.get(TELEGRAM_API.format(token=token, method='getUpdates'),
                                 params=params, timeout=25).json()
            if not resp.get('ok'):
                continue
            for upd in resp.get('result', []):
                offset = upd['update_id'] + 1
                msg = upd.get('message') or upd.get('channel_post')
                if msg and msg.get('chat'):
                    chat = msg['chat']
                    name = chat.get('username') or chat.get('first_name') or str(chat['id'])
                    with link_lock:
                        if link_state['token'] == token:
                            link_state['status']    = 'found'
                            link_state['chat_id']   = chat['id']
                            link_state['chat_name'] = name
                            link_state['active']    = False
                    return
        with link_lock:
            if link_state['token'] == token and link_state['status'] == 'listening':
                link_state['status'] = 'timeout'
                link_state['active'] = False
                _resume_main_bot_if_paused()
    except Exception as e:
        with link_lock:
            if link_state['token'] == token:
                link_state['status'] = 'error'
                link_state['error']  = str(e)
                link_state['active'] = False
                _resume_main_bot_if_paused()

def _resume_main_bot_if_paused():
    """Da chiamare tenendo link_lock. Se avevamo fermato il bot in uso per testare lo stesso token, lo rimette su."""
    if link_state['paused_main_bot']:
        link_state['paused_main_bot'] = False
        if BOT_TOKEN and CHAT_ID:
            start_bot(BOT_TOKEN, CHAT_ID)

@app.route('/api/telegram/state')
def api_telegram_state():
    connected = bool(BOT_TOKEN and CHAT_ID)
    bot_username = None
    if connected:
        try:
            bot_username = tg_get_me(BOT_TOKEN).get('username')
        except Exception:
            pass
    return jsonify({'connected': connected, 'chat_id': CHAT_ID or None, 'bot_username': bot_username})

@app.route('/api/telegram/link/start', methods=['POST'])
def api_telegram_link_start():
    token = (request.get_json(silent=True) or {}).get('token', '').strip()
    if not token:
        return jsonify({'ok': False, 'error': 'Token mancante'}), 400
    try:
        me = tg_get_me(token)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Token non valido: {e}'}), 400

    with link_lock:
        paused = False
        if token == BOT_TOKEN and bot_state.get('bot') is not None:
            with bot_state_lock:
                stop_bot()
            paused = True
        link_state.update({
            'active': True, 'token': token, 'status': 'listening',
            'chat_id': None, 'chat_name': None, 'bot_username': me.get('username'),
            'error': None, 'paused_main_bot': paused,
        })
    threading.Thread(target=_link_listen, args=(token,), daemon=True).start()
    return jsonify({'ok': True, 'bot_username': me.get('username')})

@app.route('/api/telegram/link/status')
def api_telegram_link_status():
    with link_lock:
        return jsonify({
            'status':       link_state['status'],
            'chat_id':      link_state['chat_id'],
            'chat_name':    link_state['chat_name'],
            'bot_username': link_state['bot_username'],
            'error':        link_state['error'],
        })

@app.route('/api/telegram/link/cancel', methods=['POST'])
def api_telegram_link_cancel():
    with link_lock:
        link_state['active'] = False
        link_state['status'] = 'idle'
        _resume_main_bot_if_paused()
    return jsonify({'ok': True})

@app.route('/api/telegram/link/save', methods=['POST'])
def api_telegram_link_save():
    global BOT_TOKEN, CHAT_ID
    with link_lock:
        if link_state['status'] != 'found' or not link_state['chat_id']:
            return jsonify({'ok': False, 'error': 'Nessun chat rilevata da salvare'}), 400
        token   = link_state['token']
        chat_id = str(link_state['chat_id'])
        link_state.update({'active': False, 'status': 'idle', 'paused_main_bot': False})

    save_config_values({'BOT_TOKEN': token, 'CHAT_ID': chat_id})
    BOT_TOKEN, CHAT_ID = token, chat_id
    start_bot(BOT_TOKEN, CHAT_ID)
    return jsonify({'ok': True, 'chat_id': chat_id})

@app.route('/api/telegram/unlink', methods=['POST'])
def api_telegram_unlink():
    global BOT_TOKEN, CHAT_ID
    with bot_state_lock:
        stop_bot()
    save_config_values({'BOT_TOKEN': '', 'CHAT_ID': ''})
    BOT_TOKEN, CHAT_ID = '', ''
    return jsonify({'ok': True})

# ── Status map ───────────────────────────────────────────────────────────────

STATUS_MAP = {
    'OL':      ('Online',             'online'),
    'OB':      ('Su batteria',        'onbatt'),
    'LB':      ('Batteria scarica',   'lowbatt'),
    'OL CHRG': ('Online (in carica)', 'charging'),
    'OB LB':   ('Batteria scarica',   'lowbatt'),
}

RANGE_DELTAS = {
    '1h':  timedelta(hours=1),
    '6h':  timedelta(hours=6),
    '24h': timedelta(hours=24),
    '7d':  timedelta(days=7),
    '30d': timedelta(days=30),
    '1y':  timedelta(days=365),
}

def ts_label(ts_str, rng):
    try:
        dt = datetime.fromisoformat(ts_str)
        if rng in ('1h', '6h', '24h'): return dt.strftime('%H:%M')
        if rng == '7d':                 return dt.strftime('%d/%m %H:%M')
        return dt.strftime('%d/%m')
    except Exception:
        return ts_str

def downsample(data, max_pts=500):
    if len(data) <= max_pts:
        return data
    step = len(data) / max_pts
    return [data[int(i * step)] for i in range(max_pts)]

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/api/stats')
def api_stats():
    d    = get_ups_data()
    raw  = d.get('ups.status', 'N/A')
    label, css = STATUS_MAP.get(raw, (raw, 'unknown'))
    load_pct        = d.get('ups.load', '0')
    nominal_watts   = float(d.get('ups.realpower.nominal') or 0)
    actual_watts    = round(nominal_watts * float(load_pct or 0) / 100, 1) if nominal_watts else None
    runtime_sec     = d.get('battery.runtime')
    return jsonify({
        'status_label':    label,
        'status_css':      css,
        'autonomy':        format_autonomy(runtime_sec),
        'autonomy_pct':    autonomy_pct(runtime_sec),
        'load':            load_pct,
        'actual_watts':    actual_watts,
        'uptime':          get_uptime(),
    })

@app.route('/api/history')
def api_history():
    history = read_history()
    return jsonify({
        'events': history,
        'stats':  get_blackout_stats(history),
    })

@app.route('/api/metrics')
def api_metrics():
    rng = request.args.get('range', '1h')
    if rng == 'all' or rng not in RANGE_DELTAS:
        filtered = metrics
    else:
        since    = (datetime.now() - RANGE_DELTAS[rng]).isoformat(timespec='seconds')
        filtered = [p for p in metrics if p['ts'] >= since]
    pts = downsample(filtered)
    return jsonify([{**p, 'watts': p.get('watts', 0), 'label': ts_label(p['ts'], rng)} for p in pts])

@app.route('/api/metrics/csv')
def api_metrics_csv():
    lines = ['timestamp,charge_%,load_%,watts']
    for p in metrics:
        lines.append(f"{p['ts']},{p['charge']},{p['load']},{p.get('watts', 0)}")
    return Response(
        '\n'.join(lines),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="ups-metrics.csv"'},
    )

EMERGENCY_STATE_FILE = '/var/lib/nut/emergency.state'

def test_proxmox_ssh(host, exclude_ct):
    """SSH in sola lettura verso l'host Proxmox: verifica l'accesso e lista i CT che
    ups-emergency.sh spegnerebbe (tutti tranne exclude_ct). Non modifica nulla."""
    if not host:
        return False, [], 'Host Proxmox non configurato'
    try:
        r = subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
             '-o', 'StrictHostKeyChecking=accept-new', f'root@{host}', 'pct list'],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return False, [], (r.stderr or 'SSH fallito').strip()[:300]
        cts = []
        for line in r.stdout.strip().split('\n')[1:]:
            parts = line.split()
            if parts and parts[0] != str(exclude_ct):
                cts.append({'ctid': parts[0], 'status': parts[1] if len(parts) > 1 else '?'})
        return True, cts, None
    except Exception as e:
        return False, [], str(e)

@app.route('/api/emergency/state')
def api_emergency_state():
    cfg = load_config()  # rilegge da disco: i valori possono essere stati cambiati dalla modal
    host    = cfg.get('PROXMOX_HOST', '')
    ct_id   = cfg.get('NUT_CT_ID', '')
    low     = cfg.get('THRESHOLD_RUNTIME_LOW', '')
    restore = cfg.get('THRESHOLD_RUNTIME_RESTORE', '')
    ok, cts, err = test_proxmox_ssh(host, ct_id)
    return jsonify({
        'connected':          ok,
        'active':             os.path.exists(EMERGENCY_STATE_FILE),
        'proxmox_host':       host,
        'nut_ct_id':          ct_id,
        'threshold_low':      low,
        'threshold_restore':  restore,
        'affected_cts':       cts,
        'error':              err,
    })

@app.route('/api/emergency/test', methods=['POST'])
def api_emergency_test():
    body  = request.get_json(silent=True) or {}
    host  = (body.get('proxmox_host') or '').strip()
    ct_id = (body.get('nut_ct_id') or '').strip()
    ok, cts, err = test_proxmox_ssh(host, ct_id)
    return jsonify({'ok': ok, 'affected_cts': cts, 'error': err})

@app.route('/api/emergency/save', methods=['POST'])
def api_emergency_save():
    body    = request.get_json(silent=True) or {}
    host    = (body.get('proxmox_host') or '').strip()
    ct_id   = (body.get('nut_ct_id') or '').strip()
    low     = (body.get('threshold_low') or '').strip()
    restore = (body.get('threshold_restore') or '').strip()
    if not host or not ct_id:
        return jsonify({'ok': False, 'error': "Host Proxmox e ID del CT sono obbligatori"}), 400
    try:
        low_i, restore_i = int(low), int(restore)
    except ValueError:
        return jsonify({'ok': False, 'error': 'Le soglie devono essere numeri interi (secondi)'}), 400
    if low_i <= 0 or restore_i <= 0:
        return jsonify({'ok': False, 'error': 'Le soglie devono essere maggiori di zero'}), 400
    if restore_i <= low_i:
        return jsonify({'ok': False, 'error': 'La soglia di ripristino deve essere maggiore di quella di spegnimento'}), 400

    save_config_values({
        'PROXMOX_HOST':          host,
        'NUT_CT_ID':             ct_id,
        'THRESHOLD_RUNTIME_LOW':     str(low_i),
        'THRESHOLD_RUNTIME_RESTORE': str(restore_i),
    })

    warning = None
    try:
        hw_low = float(get_ups_data().get('battery.runtime.low') or 0)
        if hw_low and low_i <= hw_low:
            warning = (f"Attenzione: la soglia di spegnimento ({low_i}s) e' minore o uguale a quella "
                       f"hardware dell'UPS ({int(hw_low)}s): l'UPS potrebbe forzare il proprio shutdown "
                       f"prima che i CT vengano spenti in ordine.")
    except Exception:
        pass
    return jsonify({'ok': True, 'warning': warning})

@app.route('/api/nut/state')
def api_nut_state():
    stanza = read_ups_stanza()
    d = get_ups_data()
    return jsonify({
        'connected': bool(d.get('ups.status')),
        'ups_name':  UPS_NAME,
        'driver':    stanza.get('driver'),
        'port':      stanza.get('port'),
        'desc':      stanza.get('desc'),
        'model':     d.get('device.model'),
        'status':    d.get('ups.status'),
    })

@app.route('/api/nut/scan', methods=['POST'])
def api_nut_scan():
    """Scansione USB in sola lettura (nut-scanner -U): non tocca ne' la config ne' i servizi."""
    try:
        r = subprocess.run(['nut-scanner', '-U'], capture_output=True, text=True, timeout=20)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    devices, current = [], None
    for line in r.stdout.splitlines():
        line = line.strip()
        m = re.match(r'^\[(.+)\]$', line)
        if m:
            if current:
                devices.append(current)
            current = {}
            continue
        if current is not None and '=' in line:
            k, v = line.split('=', 1)
            current[k.strip()] = v.strip().strip('"\'')
    if current:
        devices.append(current)
    return jsonify({'ok': True, 'devices': devices})

@app.route('/api/nut/save', methods=['POST'])
def api_nut_save():
    """Scrive driver/porta/descrizione nella stanza [myups] di ups.conf e riavvia SOLO il driver
    (nut-driver@myups): non tocca upsd/upsmon. Azione esplicita, innescata solo da click utente."""
    body   = request.get_json(silent=True) or {}
    driver = (body.get('driver') or '').strip()
    port   = (body.get('port') or '').strip()
    desc   = (body.get('desc') or '').strip()
    if not driver or not port:
        return jsonify({'ok': False, 'error': 'Driver e porta sono obbligatori'}), 400

    values = {'driver': driver, 'port': port}
    if desc:
        values['desc'] = desc
    save_ups_stanza(values)

    try:
        subprocess.run(['systemctl', 'restart', f'nut-driver@{UPS_NAME}'], check=True, timeout=20)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Configurazione salvata ma riavvio driver fallito: {e}'}), 500

    time.sleep(2)
    d  = get_ups_data()
    ok = bool(d.get('ups.status'))
    return jsonify({
        'ok':     ok,
        'status': d.get('ups.status'),
        'model':  d.get('device.model'),
        'error':  None if ok else 'Driver riavviato ma il UPS non risponde: verifica driver/porta scelti',
    })

@app.route('/')
def index():
    return HTML

# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NutController</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛡️</text></svg>">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:       #0d1117;
    --surface:  #161b22;
    --border:   #30363d;
    --text:     #e6edf3;
    --muted:    #8b949e;
    --online:   #3fb950;
    --onbatt:   #d29922;
    --lowbatt:  #f85149;
    --charging: #58a6ff;
    --unknown:  #8b949e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; min-height: 100vh; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }

  /* Header */
  header { border-bottom: 1px solid var(--border); padding: 16px 32px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap; background: linear-gradient(180deg, rgba(255,255,255,.02), transparent); }
  header h1 { font-size: 1.1rem; font-weight: 600; letter-spacing: .3px; display: flex; align-items: center; flex-shrink: 0; }
  header h1 .tag { font-size: .62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); background: var(--bg); border: 1px solid var(--border); padding: 3px 8px; border-radius: 20px; margin-left: 10px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--online); box-shadow: 0 0 6px var(--online); flex-shrink: 0; }
  header .subtitle { color: var(--muted); font-size: .82rem; margin-left: auto; text-align: right; line-height: 1.6; font-variant-numeric: tabular-nums; }

  main { max-width: 960px; margin: 0 auto; padding: 28px 20px; display: flex; flex-direction: column; gap: 24px; }

  /* Status + battery card */
  .status-card { border-radius: 12px; border: 1px solid var(--border); background: var(--surface); padding: 24px 28px; display: flex; align-items: center; gap: 24px; flex-wrap: wrap; box-shadow: 0 1px 3px rgba(0,0,0,.25); }
  .status-left  { display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
  .status-icon  { width: 56px; height: 56px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 1.7rem; background: rgba(139,148,158,.12); flex-shrink: 0; }
  .status-info .label { font-size: 1.5rem; font-weight: 700; }
  .status-info .sub   { color: var(--muted); font-size: .9rem; margin-top: 4px; }
  .status-card.online   .label { color: var(--online); }
  .status-card.online   .status-icon { background: rgba(63,185,80,.14); }
  .status-card.onbatt   .label { color: var(--onbatt); }
  .status-card.onbatt   .status-icon { background: rgba(210,153,34,.14); }
  .status-card.lowbatt  .label { color: var(--lowbatt); }
  .status-card.lowbatt  .status-icon { background: rgba(248,81,73,.14); }
  .status-card.charging .label { color: var(--charging); }
  .status-card.charging .status-icon { background: rgba(88,166,255,.14); }
  .status-card.unknown  .label { color: var(--unknown); }
  .status-divider { width: 1px; background: var(--border); align-self: stretch; flex-shrink: 0; }
  .status-right { flex: 1; min-width: 200px; }
  .batt-top  { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 10px; }
  .batt-label { font-size: .8rem; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; font-weight: 600; }
  .charge-value { font-size: 1.6rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .charge-value.charge-ok  { color: var(--online); }
  .charge-value.charge-mid { color: var(--onbatt); }
  .charge-value.charge-low { color: var(--lowbatt); }
  .bar-track { background: var(--border); border-radius: 6px; height: 8px; overflow: hidden; }
  .bar-fill  { height: 100%; border-radius: 6px; transition: width .6s ease; }
  .bar-fill.high   { background: var(--online); }
  .bar-fill.medium { background: var(--onbatt); }
  .bar-fill.low    { background: var(--lowbatt); }

  /* Stats grid */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 14px; }
  .stat { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,.25); }
  .stat .stat-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .7px; color: var(--muted); font-weight: 600; margin-bottom: 8px; }
  .stat .stat-value { font-size: 1.4rem; font-weight: 700; font-variant-numeric: tabular-nums; }
  .stat .stat-unit  { font-size: .82rem; color: var(--muted); margin-left: 3px; }

  /* Chart card */
  .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 22px 28px; box-shadow: 0 1px 3px rgba(0,0,0,.25); }
  .chart-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 18px; flex-wrap: wrap; gap: 10px; }
  .chart-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .section-title { font-size: .8rem; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); font-weight: 600; }
  .time-btns { display: flex; gap: 5px; flex-wrap: wrap; }
  .tbtn { background: none; border: 1px solid var(--border); color: var(--muted); font-size: .75rem; padding: 4px 10px; border-radius: 6px; cursor: pointer; transition: all .15s; font-family: inherit; }
  .tbtn:hover  { border-color: var(--charging); color: var(--charging); }
  .tbtn.active { border-color: var(--charging); color: var(--charging); background: rgba(88,166,255,.1); }
  .btn-export { background: none; border: 1px solid var(--border); color: var(--muted); font-size: .75rem; padding: 4px 12px; border-radius: 6px; cursor: pointer; font-family: inherit; transition: all .15s; }
  .btn-export:hover { border-color: var(--online); color: var(--online); }
  .chart-empty { text-align: center; padding: 40px; color: var(--muted); font-size: .9rem; }

  /* Chart panels (small multiples, one axis each) */
  .chart-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }
  .chart-panel .panel-title { font-size: .72rem; text-transform: uppercase; letter-spacing: .7px; color: var(--muted); font-weight: 600; margin-bottom: 10px; }

  /* Blackout summary */
  .bk-summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; margin-bottom: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.25); }
  .bk-item { padding: 14px 18px; border-right: 1px solid var(--border); }
  .bk-item:last-child { border-right: none; }
  .bk-label { font-size: .72rem; text-transform: uppercase; letter-spacing: .7px; color: var(--muted); font-weight: 600; margin-bottom: 5px; }
  .bk-value { font-size: 1.25rem; font-weight: 700; }
  .bk-item.clickable { cursor: pointer; transition: background .15s; }
  .bk-item.clickable:hover { background: rgba(255,255,255,.04); }

  /* History table */
  table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; border: 1px solid var(--border); box-shadow: 0 1px 3px rgba(0,0,0,.25); }
  th { text-align: left; padding: 12px 16px; font-size: .75rem; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 600; cursor: pointer; user-select: none; white-space: nowrap; }
  th:hover { color: var(--text); }
  th.sort-asc::after  { content: ' ▲'; color: var(--charging); }
  th.sort-desc::after { content: ' ▼'; color: var(--charging); }
  tr.row-highlight td { animation: rowFlash 2s ease; }
  @keyframes rowFlash {
    0%   { background: rgba(248,81,73,.35); }
    100% { background: transparent; }
  }
  td { padding: 13px 16px; font-size: .9rem; border-bottom: 1px solid var(--border); }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2230; }
  .no-data { text-align: center; padding: 28px; color: var(--muted); }

  .pulse { animation: pulse .4s ease; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .settings-col { display: flex; flex-direction: column; align-items: stretch; gap: 8px; flex-shrink: 0; }
  .settings-btn {
    justify-content: center;
    display: flex; align-items: center; gap: 7px; font-family: inherit; font-size: .78rem; font-weight: 600;
    padding: 6px 12px; border-radius: 20px; cursor: pointer; transition: all .15s;
    background: var(--surface); white-space: nowrap;
  }
  .settings-btn .sdot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .settings-btn.state-off  { border: 1px solid rgba(248,81,73,.4);  color: var(--lowbatt); }
  .settings-btn.state-off  .sdot { background: var(--lowbatt); box-shadow: 0 0 5px var(--lowbatt); }
  .settings-btn.state-off:hover  { background: rgba(248,81,73,.1); }
  .settings-btn.state-on   { border: 1px solid rgba(63,185,80,.4);  color: var(--online); }
  .settings-btn.state-on   .sdot { background: var(--online); box-shadow: 0 0 5px var(--online); }
  .settings-btn.state-on:hover   { background: rgba(63,185,80,.1); }
  .settings-btn.state-warn { border: 1px solid rgba(210,153,34,.5); color: var(--onbatt); }
  .settings-btn.state-warn .sdot { background: var(--onbatt); box-shadow: 0 0 5px var(--onbatt); }
  .settings-btn.state-warn:hover { background: rgba(210,153,34,.1); }

  /* Modal */
  .modal-overlay {
    display: none; position: fixed; inset: 0; background: rgba(1,4,9,.65); backdrop-filter: blur(2px);
    z-index: 100; align-items: center; justify-content: center; padding: 20px;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--border); border-radius: 14px; width: 100%;
    max-width: 440px; max-height: 88vh; overflow-y: auto; box-shadow: 0 12px 40px rgba(0,0,0,.5);
  }
  .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 18px 22px; border-bottom: 1px solid var(--border); }
  .modal-header h2 { font-size: 1.02rem; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .modal-close { background: none; border: none; color: var(--muted); font-size: 1.4rem; line-height: 1; cursor: pointer; padding: 2px 6px; border-radius: 6px; }
  .modal-close:hover { color: var(--text); background: rgba(255,255,255,.06); }
  .modal-body { padding: 20px 22px 24px; display: flex; flex-direction: column; gap: 16px; }
  .modal-body label { font-size: .78rem; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; display: block; }
  .modal-body input[type=text], .modal-body input[type=password] {
    width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text);
    padding: 10px 12px; border-radius: 8px; font-family: inherit; font-size: .9rem; font-variant-numeric: tabular-nums;
  }
  .modal-body input:focus { outline: none; border-color: var(--charging); }
  .modal-hint { font-size: .78rem; color: var(--muted); line-height: 1.5; }
  .modal-hint a { color: var(--charging); }
  .modal-body code { background: var(--bg); border: 1px solid var(--border); padding: 1px 5px; border-radius: 4px; font-size: .85em; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .modal-actions { display: flex; gap: 10px; margin-top: 4px; flex-wrap: wrap; }
  .mbtn { font-family: inherit; font-size: .85rem; font-weight: 600; padding: 9px 16px; border-radius: 8px; cursor: pointer; border: 1px solid var(--border); background: none; color: var(--text); transition: all .15s; }
  .mbtn:hover:not(:disabled) { border-color: var(--charging); color: var(--charging); }
  .mbtn:disabled { opacity: .5; cursor: not-allowed; }
  .mbtn-primary { background: var(--charging); border-color: var(--charging); color: #04121f; }
  .mbtn-primary:hover:not(:disabled) { filter: brightness(1.08); color: #04121f; }
  .mbtn-danger { border-color: rgba(248,81,73,.5); color: var(--lowbatt); }
  .mbtn-danger:hover:not(:disabled) { background: rgba(248,81,73,.1); border-color: var(--lowbatt); }
  .status-box { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; font-size: .86rem; display: flex; align-items: flex-start; gap: 10px; line-height: 1.5; }
  .status-box.ok      { border-color: rgba(63,185,80,.4);  background: rgba(63,185,80,.08); }
  .status-box.warn     { border-color: rgba(210,153,34,.4); background: rgba(210,153,34,.08); }
  .status-box.error   { border-color: rgba(248,81,73,.4);  background: rgba(248,81,73,.08); }
  .status-box.busy    { border-color: rgba(88,166,255,.4); background: rgba(88,166,255,.08); }
  /* Il contenuto testuale va sempre in un unico blocco: display:flex sul contenitore
     spezzerebbe altrimenti testo/<br>/<strong> in piu' flex-item con gap indesiderati tra loro. */
  .status-box .sb-icon { flex-shrink: 0; }
  .status-box .sb-content { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .spinner { width: 14px; height: 14px; border: 2px solid rgba(88,166,255,.3); border-top-color: var(--charging); border-radius: 50%; animation: spin .7s linear infinite; flex-shrink: 0; margin-top: 2px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .device-option { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; cursor: pointer; font-size: .85rem; transition: all .15s; }
  .device-option:hover, .device-option.selected { border-color: var(--charging); background: rgba(88,166,255,.08); }
  .device-option .dev-title { font-weight: 600; margin-bottom: 2px; }
  .device-option .dev-sub { color: var(--muted); font-size: .78rem; }

  @media (max-width: 620px) {
    header { padding: 14px 16px; }
    header h1 { font-size: 1rem; }
    header .subtitle { font-size: .75rem; }
    .settings-col { width: 100%; }
    .settings-col .settings-btn { width: 100%; justify-content: center; font-size: .74rem; padding: 7px 10px; }
    main { padding: 14px 12px; gap: 16px; }
    .status-card { flex-direction: column; gap: 16px; }
    .status-divider { width: 100%; height: 1px; align-self: stretch; }
    .status-right { min-width: unset; width: 100%; }
    .status-info .label { font-size: 1.3rem; }
    .grid { grid-template-columns: repeat(2, 1fr); }
    .chart-grid { grid-template-columns: 1fr; gap: 26px; }
    .bk-summary { grid-template-columns: repeat(2, 1fr); }
    .bk-item { border-right: none; border-bottom: 1px solid var(--border); }
    .bk-item:nth-child(odd) { border-right: 1px solid var(--border); }
    .bk-item:last-child, .bk-item:nth-last-child(2):nth-child(odd) { border-bottom: none; }
    .chart-card { padding: 16px; }
    .chart-header { flex-direction: column; align-items: flex-start; }
    td, th { padding: 10px 10px; font-size: .82rem; }
    .info-card { padding: 16px; }
  }
</style>
</head>
<body>

<header>
  <div class="dot" id="header-dot"></div>
  <h1>NutController<span class="tag">UPS Monitor</span></h1>
  <span class="subtitle">
    Aggiornato: <span id="last-update">—</span><br>
    Ora: <span id="live-clock">—</span>
  </span>
</header>

<!-- Modal: Telegram -->
<div class="modal-overlay" id="tg-modal-overlay">
  <div class="modal">
    <div class="modal-header">
      <h2>💬 Collega Telegram</h2>
      <button class="modal-close" id="tg-modal-close">&times;</button>
    </div>
    <div class="modal-body" id="tg-modal-body"><!-- popolato via JS --></div>
  </div>
</div>

<!-- Modal: NUT -->
<div class="modal-overlay" id="nut-modal-overlay">
  <div class="modal">
    <div class="modal-header">
      <h2>🔌 Collega NUT</h2>
      <button class="modal-close" id="nut-modal-close">&times;</button>
    </div>
    <div class="modal-body" id="nut-modal-body"><!-- popolato via JS --></div>
  </div>
</div>

<!-- Modal: Emergenza UPS -->
<div class="modal-overlay" id="emg-modal-overlay">
  <div class="modal">
    <div class="modal-header">
      <h2>⚡ Emergenza UPS</h2>
      <button class="modal-close" id="emg-modal-close">&times;</button>
    </div>
    <div class="modal-body" id="emg-modal-body"><!-- popolato via JS --></div>
  </div>
</div>

<main>
  <!-- Status + battery -->
  <div class="status-card" id="status-card">
    <div class="status-left">
      <div class="status-icon" id="status-icon">⏳</div>
      <div class="status-info">
        <div class="label" id="status-label">Caricamento…</div>
        <div class="sub">UPS · myups</div>
      </div>
    </div>
    <div class="status-divider"></div>
    <div class="status-right">
      <div class="batt-top">
        <span class="batt-label">Autonomia stimata</span>
        <span class="charge-value" id="autonomy-value">—</span>
      </div>
      <div class="bar-track"><div class="bar-fill" id="bar-fill" style="width:0%"></div></div>
    </div>
    <div class="status-divider"></div>
    <div class="settings-col">
      <button class="settings-btn state-off" id="tg-settings-btn"><span class="sdot"></span><span id="tg-settings-label">Collega Telegram</span></button>
      <button class="settings-btn state-off" id="nut-settings-btn"><span class="sdot"></span><span id="nut-settings-label">Collega NUT</span></button>
      <button class="settings-btn state-off" id="emg-settings-btn"><span class="sdot"></span><span id="emg-settings-label">Emergenza UPS</span></button>
    </div>
  </div>

  <!-- Stats grid -->
  <div class="grid">
    <div class="stat">
      <div class="stat-label">Carico UPS</div>
      <div class="stat-value"><span id="load">—</span><span class="stat-unit">%</span></div>
    </div>
    <div class="stat">
      <div class="stat-label">Consumo stimato</div>
      <div class="stat-value"><span id="watts">—</span><span class="stat-unit">W</span></div>
    </div>
    <div class="stat">
      <div class="stat-label">Uptime sistema</div>
      <div class="stat-value" style="font-size:1.05rem" id="uptime">—</div>
    </div>
  </div>

  <!-- Chart -->
  <div class="chart-card">
    <div class="chart-header">
      <span class="section-title">Andamento</span>
      <div class="chart-actions">
        <div class="time-btns">
          <button class="tbtn" data-range="1h">1h</button>
          <button class="tbtn" data-range="6h">6h</button>
          <button class="tbtn" data-range="24h">24h</button>
          <button class="tbtn" data-range="7d">7 giorni</button>
          <button class="tbtn active" data-range="30d">1 mese</button>
          <button class="tbtn" data-range="1y">1 anno</button>
          <button class="tbtn" data-range="all">Tutto</button>
        </div>
        <button class="btn-export" onclick="exportCSV()">⬇ CSV</button>
      </div>
    </div>
    <div class="chart-grid">
      <div class="chart-panel">
        <div class="panel-title">Carica batteria</div>
        <div id="chart-wrap-charge"><canvas id="chart-charge"></canvas></div>
      </div>
      <div class="chart-panel">
        <div class="panel-title">Consumo energetico</div>
        <div id="chart-wrap-watts"><canvas id="chart-watts"></canvas></div>
      </div>
    </div>
  </div>

  <!-- Blackout summary + history -->
  <div>
    <div class="section-title" style="margin-bottom:12px">Storico blackout</div>
    <div class="bk-summary" id="bk-summary">
      <div class="bk-item"><div class="bk-label">Totale eventi</div><div class="bk-value" id="bk-total">—</div></div>
      <div class="bk-item"><div class="bk-label">Durata media</div><div class="bk-value" id="bk-avg">—</div></div>
      <div class="bk-item clickable" id="bk-max-tile" title="Vai all'evento nello storico"><div class="bk-label">Peggiore</div><div class="bk-value" id="bk-max">—</div><div style="font-size:.75rem;color:var(--muted);margin-top:3px" id="bk-max-date"></div></div>
      <div class="bk-item"><div class="bk-label">Ultimo evento</div><div class="bk-value" style="font-size:.9rem" id="bk-last">—</div></div>
    </div>
    <table>
      <thead>
        <tr>
          <th id="th-id"    class="sort-desc">#</th>
          <th id="th-start">Inizio</th>
          <th id="th-end">Fine</th>
          <th id="th-dur">Durata</th>
        </tr>
      </thead>
      <tbody id="history-body"><tr><td colspan="4" class="no-data">Caricamento…</td></tr></tbody>
    </table>
  </div>
</main>

<script>
const LOAD_WATTS   = 44;
const STATUS_ICONS = { online:'✅', onbatt:'⚠️', lowbatt:'🔴', charging:'🔌', unknown:'❓' };
let chartRange = '30d';
// ── Clock ─────────────────────────────────────────────────────────────────────
setInterval(() => {
  document.getElementById('live-clock').textContent = new Date().toLocaleTimeString('it-IT');
}, 1000);
document.getElementById('live-clock').textContent = new Date().toLocaleTimeString('it-IT');

// ── Stats ─────────────────────────────────────────────────────────────────────
async function fetchStats() {
  try {
    const d   = await fetch('/api/stats').then(r => r.json());
    const css = d.status_css;

    document.getElementById('status-card').className  = 'status-card ' + css;
    document.getElementById('status-icon').textContent  = STATUS_ICONS[css] || '❓';
    document.getElementById('status-label').textContent = d.status_label;

    const dot = document.getElementById('header-dot');
    const col = css === 'online' || css === 'charging' ? 'var(--online)' : css === 'onbatt' ? 'var(--onbatt)' : 'var(--lowbatt)';
    dot.style.background = col;
    dot.style.boxShadow  = `0 0 6px ${col}`;

    // Autonomia stimata (da battery.runtime reale dell'UPS)
    const pct = d.autonomy_pct || 0;
    const autonomyEl = document.getElementById('autonomy-value');
    autonomyEl.className = 'charge-value ' + (pct > 60 ? 'charge-ok' : pct > 30 ? 'charge-mid' : 'charge-low');
    autonomyEl.textContent = d.autonomy;

    const bar = document.getElementById('bar-fill');
    bar.style.width = pct + '%';
    bar.className   = 'bar-fill ' + (pct > 60 ? 'high' : pct > 30 ? 'medium' : 'low');

    document.getElementById('load').textContent    = d.load;
    document.getElementById('uptime').textContent  = d.uptime;
    document.getElementById('watts').textContent   = d.actual_watts !== null ? d.actual_watts : LOAD_WATTS;

    const el = document.getElementById('last-update');
    el.textContent = new Date().toLocaleTimeString('it-IT');
    el.classList.remove('pulse');
    void el.offsetWidth;
    el.classList.add('pulse');
  } catch(e) {}
}

// ── Chart ─────────────────────────────────────────────────────────────────────
function baseChartOptions(extraTickOpts) {
  return {
    animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { backgroundColor: '#1c2230', titleColor: '#e6edf3', bodyColor: '#8b949e', borderColor: '#30363d', borderWidth: 1 },
    },
    scales: {
      x: { ticks: { color: '#8b949e', maxTicksLimit: 8, font: { size: 11 } }, grid: { color: '#21262d' } },
      y: Object.assign({ ticks: { color: '#8b949e', font: { size: 11 } }, grid: { color: '#21262d' } }, extraTickOpts || {}),
    },
  };
}

function renderPanel(wrapId, canvasId, chartRef, labels, values, cfg) {
  const wrap = document.getElementById(wrapId);
  if (!labels.length) {
    if (chartRef.chart) { chartRef.chart.destroy(); chartRef.chart = null; }
    wrap.innerHTML = '<div class="chart-empty">Nessun dato disponibile per questo intervallo</div>';
    return;
  }
  if (!wrap.querySelector('canvas')) {
    wrap.innerHTML = `<canvas id="${canvasId}"></canvas>`;
  }
  if (!chartRef.chart) {
    chartRef.chart = new Chart(document.getElementById(canvasId).getContext('2d'), {
      type: 'line',
      data: { labels, datasets: [{ data: values, borderColor: cfg.color, backgroundColor: cfg.fillColor, fill: true, borderWidth: 2, tension: 0.3, pointRadius: 0 }] },
      options: baseChartOptions(cfg.yOpts),
    });
  } else {
    chartRef.chart.data.labels             = labels;
    chartRef.chart.data.datasets[0].data   = values;
    chartRef.chart.update('none');
  }
}

const chargeChartRef = {}, wattsChartRef = {};

async function fetchMetrics() {
  try {
    const data   = await fetch('/api/metrics?range=' + chartRange).then(r => r.json());
    const labels = data.map(p => p.label);

    renderPanel('chart-wrap-charge', 'chart-charge', chargeChartRef, labels, data.map(p => p.charge), {
      color: '#58a6ff', fillColor: 'rgba(88,166,255,.08)', yOpts: { min: 0, max: 100 },
    });
    renderPanel('chart-wrap-watts', 'chart-watts', wattsChartRef, labels, data.map(p => p.watts || 0), {
      color: '#e3b341', fillColor: 'rgba(227,179,65,.08)', yOpts: { ticks: { color: '#8b949e', font: { size: 11 }, callback: v => v + 'W' } },
    });
  } catch(e) {}
}

document.querySelectorAll('.tbtn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    chartRange = btn.dataset.range;
    if (chargeChartRef.chart) { chargeChartRef.chart.destroy(); chargeChartRef.chart = null; }
    if (wattsChartRef.chart)  { wattsChartRef.chart.destroy();  wattsChartRef.chart  = null; }
    fetchMetrics();
  });
});

function exportCSV() {
  window.location.href = '/api/metrics/csv';
}

// ── History ───────────────────────────────────────────────────────────────────
let historyData = [], sortCol = 'id', sortDir = 'desc', worstStart = null;
const COL_TH = { id: 'th-id', start: 'th-start', end: 'th-end', duration: 'th-dur' };

function scrollToWorst() {
  if (!worstStart) return;
  const row = document.querySelector(`tr[data-start="${worstStart}"]`);
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  row.classList.remove('row-highlight');
  void row.offsetWidth;
  row.classList.add('row-highlight');
}
document.getElementById('bk-max-tile').addEventListener('click', scrollToWorst);

function durToSec(str) {
  if (!str) return -1;
  const old = str.match(/^(\d+)\s*minut/);
  if (old) return parseInt(old[1]) * 60;
  let s = 0;
  const h  = str.match(/(\d+)h/);  if (h)  s += parseInt(h[1])  * 3600;
  const m  = str.match(/(\d+)m/);  if (m)  s += parseInt(m[1])  * 60;
  const sc = str.match(/(\d+)s/);  if (sc) s += parseInt(sc[1]);
  return s;
}

function renderHistory() {
  const data = historyData.map((e, i) => ({ ...e, _id: historyData.length - i }));
  data.sort((a, b) => {
    let va, vb;
    if      (sortCol === 'id')       { va = a._id;         vb = b._id; }
    else if (sortCol === 'start')    { va = a.start || '';  vb = b.start || ''; }
    else if (sortCol === 'end')      { va = a.end   || '';  vb = b.end   || ''; }
    else if (sortCol === 'duration') { va = durToSec(a.duration); vb = durToSec(b.duration); }
    if (va < vb) return sortDir === 'asc' ? -1 :  1;
    if (va > vb) return sortDir === 'asc' ?  1 : -1;
    return 0;
  });
  Object.entries(COL_TH).forEach(([col, id]) => {
    document.getElementById(id).className = col === sortCol ? 'sort-' + sortDir : '';
  });
  const tbody = document.getElementById('history-body');
  tbody.innerHTML = data.length
    ? data.map(e => `<tr data-start="${e.start || ''}">
        <td style="color:var(--muted)">${e._id}</td>
        <td>${e.start || '—'}</td>
        <td>${e.end || (e.orphaned ? '<span style="color:var(--muted)">Fine non presente</span>' : '<span style="color:var(--onbatt)">In corso…</span>')}</td>
        <td><strong>${e.duration || '—'}</strong></td>
      </tr>`).join('')
    : '<tr><td colspan="4" class="no-data">Nessun blackout registrato</td></tr>';
}

document.querySelectorAll('#th-id,#th-start,#th-end,#th-dur').forEach(th => {
  const map = { 'th-id':'id', 'th-start':'start', 'th-end':'end', 'th-dur':'duration' };
  th.addEventListener('click', () => {
    sortDir = sortCol === map[th.id] ? (sortDir === 'asc' ? 'desc' : 'asc') : 'desc';
    sortCol = map[th.id];
    renderHistory();
  });
});

async function fetchHistory() {
  try {
    const resp = await fetch('/api/history').then(r => r.json());
    historyData = resp.events;
    const s = resp.stats;
    document.getElementById('bk-total').textContent    = s.total    || '0';
    document.getElementById('bk-avg').textContent      = s.avg      || '—';
    document.getElementById('bk-max').textContent      = s.max      || '—';
    document.getElementById('bk-max-date').textContent = s.max_date || '';
    document.getElementById('bk-last').textContent     = s.last ? s.last.split(' ')[0] : '—';
    worstStart = s.max_start || null;
    document.getElementById('bk-max-tile').classList.toggle('clickable', !!worstStart);
    renderHistory();
  } catch(e) {}
}

// ── Helper condiviso dai 3 modal di impostazione ─────────────────────────────
// Il contenuto testuale (che puo' contenere <br>/<strong>/<code>) va sempre in un
// unico div: non lasciarlo come testo "nudo" dentro .status-box (display:flex),
// altrimenti ogni frammento di testo/tag diventa un flex-item a se' e il gap del
// contenitore li separa in modo scomposto invece di farli scorrere come testo normale.
function statusBox(cls, icon, html) {
  return `<div class="status-box ${cls}">${icon ? `<span class="sb-icon">${icon}</span>` : ''}<div class="sb-content">${html}</div></div>`;
}
function statusBoxBusy(html) {
  return `<div class="status-box busy"><div class="spinner"></div><div class="sb-content">${html}</div></div>`;
}

// ── Telegram settings modal ──────────────────────────────────────────────────
const tgOverlay = document.getElementById('tg-modal-overlay');
const tgBody    = document.getElementById('tg-modal-body');
const tgBtn     = document.getElementById('tg-settings-btn');
const tgLabel   = document.getElementById('tg-settings-label');
let tgPollTimer = null;
let tgConnectedState = { connected: false, chat_id: null, bot_username: null };

function setTgButton(connected) {
  tgBtn.className = 'settings-btn ' + (connected ? 'state-on' : 'state-off');
  tgLabel.textContent = connected ? 'Telegram collegato' : 'Collega Telegram';
}

async function refreshTgState() {
  try {
    const d = await fetch('/api/telegram/state').then(r => r.json());
    tgConnectedState = d;
    setTgButton(d.connected);
  } catch(e) {}
}

function tgRenderIdle() {
  const d = tgConnectedState;
  tgBody.innerHTML = `
    ${d.connected ? `
      ${statusBox('ok', '✅', `Collegato a <strong>@${d.bot_username || '—'}</strong><br>Chat ID: <code>${d.chat_id}</code>`)}
      <div class="modal-hint">Per cambiare bot incolla un nuovo token e premi "Collega". Per scollegare del tutto usa "Scollega".</div>
    ` : `
      <div class="modal-hint">Crea un bot con <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> su Telegram, copia il token e incollalo qui sotto. Poi scrivi un messaggio qualsiasi al bot: il chat ID viene rilevato in automatico, senza bisogno di cercarlo a mano.</div>
    `}
    <div>
      <label for="tg-token-input">Token del bot</label>
      <input type="text" id="tg-token-input" placeholder="1234567890:AA...." autocomplete="off" spellcheck="false">
    </div>
    <div class="modal-actions">
      <button class="mbtn mbtn-primary" id="tg-connect-btn">Collega</button>
      ${d.connected ? '<button class="mbtn mbtn-danger" id="tg-unlink-btn">Scollega</button>' : ''}
    </div>
  `;
  document.getElementById('tg-connect-btn').addEventListener('click', tgStartLink);
  const unlinkBtn = document.getElementById('tg-unlink-btn');
  if (unlinkBtn) unlinkBtn.addEventListener('click', tgUnlink);
}

async function tgStartLink() {
  const token = document.getElementById('tg-token-input').value.trim();
  if (!token) return;
  tgBody.innerHTML = statusBoxBusy('Verifica del token in corso…');
  try {
    const resp = await fetch('/api/telegram/link/start', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})
    });
    const d = await resp.json();
    if (!d.ok) { tgRenderError(d.error); return; }
    tgRenderListening(d.bot_username);
    tgPollTimer = setInterval(tgPollLinkStatus, 1500);
  } catch(e) { tgRenderError('Errore di rete'); }
}

function tgRenderListening(botUsername) {
  tgBody.innerHTML = `
    ${statusBoxBusy(`In ascolto… apri Telegram, cerca <strong>@${botUsername}</strong> e invia un messaggio qualsiasi (es. /start). Rilevamento entro 2 minuti.`)}
    <div class="modal-actions"><button class="mbtn" id="tg-cancel-btn">Annulla</button></div>
  `;
  document.getElementById('tg-cancel-btn').addEventListener('click', tgCancelLink);
}

async function tgPollLinkStatus() {
  try {
    const d = await fetch('/api/telegram/link/status').then(r => r.json());
    if (d.status === 'found') {
      clearInterval(tgPollTimer); tgPollTimer = null;
      tgRenderFound(d);
    } else if (d.status === 'timeout') {
      clearInterval(tgPollTimer); tgPollTimer = null;
      tgRenderError('Nessun messaggio ricevuto entro il tempo massimo. Riprova.');
    } else if (d.status === 'error') {
      clearInterval(tgPollTimer); tgPollTimer = null;
      tgRenderError(d.error || 'Errore sconosciuto');
    }
  } catch(e) {}
}

function tgRenderFound(d) {
  tgBody.innerHTML = `
    ${statusBox('ok', '✅', `Rilevato: <strong>${d.chat_name}</strong> (chat ID <code>${d.chat_id}</code>)<br>Bot: @${d.bot_username}`)}
    <div class="modal-actions">
      <button class="mbtn mbtn-primary" id="tg-save-btn">Salva</button>
      <button class="mbtn" id="tg-discard-btn">Annulla</button>
    </div>
  `;
  document.getElementById('tg-save-btn').addEventListener('click', tgSaveLink);
  document.getElementById('tg-discard-btn').addEventListener('click', tgCancelLink);
}

async function tgSaveLink() {
  tgBody.innerHTML = statusBoxBusy('Salvataggio…');
  try {
    const resp = await fetch('/api/telegram/link/save', { method: 'POST' });
    const d = await resp.json();
    if (!d.ok) { tgRenderError(d.error); return; }
    await refreshTgState();
    tgRenderIdle();
  } catch(e) { tgRenderError('Errore di rete durante il salvataggio'); }
}

async function tgCancelLink() {
  if (tgPollTimer) { clearInterval(tgPollTimer); tgPollTimer = null; }
  try { await fetch('/api/telegram/link/cancel', { method: 'POST' }); } catch(e) {}
  tgRenderIdle();
}

async function tgUnlink() {
  if (!confirm("Scollegare Telegram? Il bot smettera' di rispondere finche' non ricolleghi.")) return;
  try {
    await fetch('/api/telegram/unlink', { method: 'POST' });
    await refreshTgState();
    tgRenderIdle();
  } catch(e) {}
}

function tgRenderError(msg) {
  tgBody.innerHTML = `
    ${statusBox('error', '⚠️', msg)}
    <div class="modal-actions"><button class="mbtn mbtn-primary" id="tg-retry-btn">Riprova</button></div>
  `;
  document.getElementById('tg-retry-btn').addEventListener('click', tgRenderIdle);
}

function openTgModal() {
  tgOverlay.classList.add('open');
  refreshTgState().then(tgRenderIdle);
}
function closeTgModal() {
  if (tgPollTimer) { tgCancelLink(); }
  tgOverlay.classList.remove('open');
}
tgBtn.addEventListener('click', openTgModal);
document.getElementById('tg-modal-close').addEventListener('click', closeTgModal);
tgOverlay.addEventListener('click', e => { if (e.target === tgOverlay) closeTgModal(); });

// ── NUT settings modal ───────────────────────────────────────────────────────
const nutOverlay = document.getElementById('nut-modal-overlay');
const nutBody    = document.getElementById('nut-modal-body');
const nutBtn     = document.getElementById('nut-settings-btn');
const nutLabel   = document.getElementById('nut-settings-label');
let nutState   = { connected: false };
let nutScanned = [];

function setNutButton(connected) {
  nutBtn.className = 'settings-btn ' + (connected ? 'state-on' : 'state-off');
  nutLabel.textContent = connected ? 'NUT collegato' : 'Collega NUT';
}

async function refreshNutState() {
  try {
    const d = await fetch('/api/nut/state').then(r => r.json());
    nutState = d;
    setNutButton(d.connected);
  } catch(e) {}
}

function nutRenderIdle() {
  const d = nutState;
  nutBody.innerHTML = `
    ${statusBox(d.connected ? 'ok' : 'error', d.connected ? '✅' : '⚠️',
      `${d.connected ? `UPS attivo (${d.model || d.status || 'online'})` : 'UPS non raggiungibile'}<br>Driver: <strong>${d.driver || '—'}</strong> · Porta: <strong>${d.port || '—'}</strong>${d.desc ? ' · ' + d.desc : ''}`)}
    <div class="modal-hint">Se l'UPS non risponde o hai cambiato dispositivo USB, avvia la rilevazione: lo scan e' in sola lettura e non modifica nulla finche' non premi "Salva e riavvia driver".</div>
    <div class="modal-actions">
      <button class="mbtn mbtn-primary" id="nut-scan-btn">Rileva UPS collegate (USB)</button>
    </div>
    <div id="nut-devices"></div>
  `;
  document.getElementById('nut-scan-btn').addEventListener('click', nutScan);
}

async function nutScan() {
  const devicesEl = document.getElementById('nut-devices');
  devicesEl.innerHTML = statusBoxBusy('Scansione USB in corso…');
  try {
    const resp = await fetch('/api/nut/scan', { method: 'POST' });
    const d = await resp.json();
    if (!d.ok) { devicesEl.innerHTML = statusBox('error', '⚠️', d.error); return; }
    nutScanned = d.devices || [];
    if (!nutScanned.length) {
      devicesEl.innerHTML = statusBox('warn', '⚠️', 'Nessun dispositivo USB rilevato. Controlla il cavo e riprova.');
      return;
    }
    devicesEl.innerHTML = nutScanned.map((dev, i) => `
      <div class="device-option" data-idx="${i}" style="margin-bottom:8px">
        <div class="dev-title">${dev.product || dev.driver || 'Dispositivo UPS'}</div>
        <div class="dev-sub">driver ${dev.driver || '?'} · porta ${dev.port || '?'}${dev.vendorid ? ' · USB ' + dev.vendorid + ':' + (dev.productid||'') : ''}</div>
      </div>
    `).join('') + `<div class="modal-actions" style="margin-top:10px"><button class="mbtn mbtn-primary" id="nut-configure-btn" disabled>Configura il dispositivo selezionato</button></div>`;
    devicesEl.querySelectorAll('.device-option').forEach(el => {
      el.addEventListener('click', () => {
        devicesEl.querySelectorAll('.device-option').forEach(o => o.classList.remove('selected'));
        el.classList.add('selected');
        const cfgBtn = document.getElementById('nut-configure-btn');
        cfgBtn.disabled = false;
        cfgBtn.dataset.idx = el.dataset.idx;
      });
    });
    document.getElementById('nut-configure-btn').addEventListener('click', e => {
      const idx = parseInt(e.target.dataset.idx, 10);
      nutRenderConfirm(nutScanned[idx]);
    });
  } catch(e) {
    devicesEl.innerHTML = statusBox('error', '⚠️', 'Errore di rete durante la scansione');
  }
}

function nutRenderConfirm(dev) {
  nutBody.innerHTML = `
    <div class="modal-hint">Controlla i valori rilevati (modificabili se necessario) e premi "Salva" per scriverli in <code>ups.conf</code> e riavviare il driver NUT.</div>
    <div>
      <label for="nut-driver-input">Driver</label>
      <input type="text" id="nut-driver-input" value="${dev.driver || 'usbhid-ups'}">
    </div>
    <div>
      <label for="nut-port-input">Porta</label>
      <input type="text" id="nut-port-input" value="${dev.port || 'auto'}">
    </div>
    <div>
      <label for="nut-desc-input">Descrizione (opzionale)</label>
      <input type="text" id="nut-desc-input" value="${nutState.desc || ''}" placeholder="es. Il mio UPS">
    </div>
    <div class="modal-actions">
      <button class="mbtn mbtn-primary" id="nut-save-btn">Salva e riavvia driver</button>
      <button class="mbtn" id="nut-back-btn">Indietro</button>
    </div>
  `;
  document.getElementById('nut-save-btn').addEventListener('click', nutSave);
  document.getElementById('nut-back-btn').addEventListener('click', nutRenderIdle);
}

async function nutSave() {
  const driver = document.getElementById('nut-driver-input').value.trim();
  const port   = document.getElementById('nut-port-input').value.trim();
  const desc   = document.getElementById('nut-desc-input').value.trim();
  nutBody.innerHTML = statusBoxBusy('Salvataggio e riavvio driver in corso… (qualche secondo di interruzione monitoraggio)');
  try {
    const resp = await fetch('/api/nut/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({driver, port, desc})
    });
    const d = await resp.json();
    await refreshNutState();
    if (!d.ok) {
      nutBody.innerHTML = statusBox('error', '⚠️', d.error) + '<div class="modal-actions"><button class="mbtn mbtn-primary" id="nut-retry-btn">Torna alle impostazioni</button></div>';
      document.getElementById('nut-retry-btn').addEventListener('click', nutRenderIdle);
      return;
    }
    nutRenderIdle();
  } catch(e) {
    nutBody.innerHTML = statusBox('error', '⚠️', 'Errore di rete durante il salvataggio');
  }
}

function openNutModal() {
  nutOverlay.classList.add('open');
  refreshNutState().then(nutRenderIdle);
}
function closeNutModal() {
  nutOverlay.classList.remove('open');
}
nutBtn.addEventListener('click', openNutModal);
document.getElementById('nut-modal-close').addEventListener('click', closeNutModal);
nutOverlay.addEventListener('click', e => { if (e.target === nutOverlay) closeNutModal(); });

// ── Emergenza UPS settings modal ─────────────────────────────────────────────
const emgOverlay = document.getElementById('emg-modal-overlay');
const emgBody    = document.getElementById('emg-modal-body');
const emgBtn     = document.getElementById('emg-settings-btn');
const emgLabel   = document.getElementById('emg-settings-label');
let emgState = { connected: false, active: false };

function setEmgButton(d) {
  let cls = 'state-off', label = 'Emergenza UPS';
  if (d.active)          { cls = 'state-warn'; label = 'Emergenza attiva'; }
  else if (d.connected)  { cls = 'state-on';   label = 'Emergenza UPS'; }
  emgBtn.className = 'settings-btn ' + cls;
  emgLabel.textContent = label;
}

async function refreshEmgState() {
  try {
    const d = await fetch('/api/emergency/state').then(r => r.json());
    emgState = d;
    setEmgButton(d);
  } catch(e) {}
}

function emgAffectedHtml(cts) {
  if (!cts || !cts.length) return '<div class="modal-hint">Nessun altro CT trovato su questo host.</div>';
  return '<div class="modal-hint">CT che verrebbero spenti in emergenza: ' +
    cts.map(c => `#${c.ctid} (${c.status})`).join(', ') + '</div>';
}

function emgRenderIdle() {
  const d = emgState;
  emgBody.innerHTML = `
    ${d.active ? statusBox('warn', '⚠️', 'Emergenza attualmente <strong>attiva</strong>: gli altri CT sono stati spenti in attesa del ripristino corrente.') : ''}
    ${statusBox(d.connected ? 'ok' : 'error', d.connected ? '✅' : '⚠️',
      d.connected ? 'Connessione SSH a Proxmox ok' : ('Connessione SSH non riuscita' + (d.error ? ': ' + d.error : '')))}
    ${emgAffectedHtml(d.affected_cts)}
    <div class="modal-hint">Questi valori controllano quando NutController spegne (e poi riaccende) tutti gli altri CT dell'host Proxmox durante un blackout prolungato. Le soglie sono in secondi di autonomia stimata (<code>battery.runtime</code>).</div>
    <div>
      <label for="emg-host-input">Host Proxmox (IP)</label>
      <input type="text" id="emg-host-input" value="${d.proxmox_host || ''}" placeholder="192.168.0.70">
    </div>
    <div>
      <label for="emg-ctid-input">ID di questo CT (escluso dallo spegnimento)</label>
      <input type="text" id="emg-ctid-input" value="${d.nut_ct_id || ''}" placeholder="111">
    </div>
    <div>
      <label for="emg-low-input">Soglia spegnimento (secondi)</label>
      <input type="text" id="emg-low-input" value="${d.threshold_low || ''}" placeholder="600">
    </div>
    <div>
      <label for="emg-restore-input">Soglia ripristino (secondi)</label>
      <input type="text" id="emg-restore-input" value="${d.threshold_restore || ''}" placeholder="1200">
    </div>
    <div class="modal-actions">
      <button class="mbtn" id="emg-test-btn">Testa connessione SSH</button>
      <button class="mbtn mbtn-primary" id="emg-save-btn">Salva</button>
    </div>
    <div id="emg-test-result"></div>
  `;
  document.getElementById('emg-test-btn').addEventListener('click', emgTest);
  document.getElementById('emg-save-btn').addEventListener('click', emgSave);
}

async function emgTest() {
  const host  = document.getElementById('emg-host-input').value.trim();
  const ctId  = document.getElementById('emg-ctid-input').value.trim();
  const resEl = document.getElementById('emg-test-result');
  resEl.innerHTML = statusBoxBusy('Test connessione in corso…');
  try {
    const resp = await fetch('/api/emergency/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({proxmox_host: host, nut_ct_id: ctId})
    });
    const d = await resp.json();
    resEl.innerHTML = d.ok
      ? statusBox('ok', '✅', 'Connesso.') + emgAffectedHtml(d.affected_cts)
      : statusBox('error', '⚠️', d.error || 'Connessione fallita');
  } catch(e) {
    resEl.innerHTML = statusBox('error', '⚠️', 'Errore di rete');
  }
}

async function emgSave() {
  const body = {
    proxmox_host:      document.getElementById('emg-host-input').value.trim(),
    nut_ct_id:         document.getElementById('emg-ctid-input').value.trim(),
    threshold_low:     document.getElementById('emg-low-input').value.trim(),
    threshold_restore: document.getElementById('emg-restore-input').value.trim(),
  };
  const resEl = document.getElementById('emg-test-result');
  resEl.innerHTML = statusBoxBusy('Salvataggio…');
  try {
    const resp = await fetch('/api/emergency/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const d = await resp.json();
    if (!d.ok) { resEl.innerHTML = statusBox('error', '⚠️', d.error); return; }
    await refreshEmgState();
    resEl.innerHTML = statusBox('ok', '✅', `Configurazione salvata.${d.warning ? '<br>⚠️ ' + d.warning : ''}`);
  } catch(e) {
    resEl.innerHTML = statusBox('error', '⚠️', 'Errore di rete durante il salvataggio');
  }
}

function openEmgModal() {
  emgOverlay.classList.add('open');
  refreshEmgState().then(emgRenderIdle);
}
function closeEmgModal() {
  emgOverlay.classList.remove('open');
}
emgBtn.addEventListener('click', openEmgModal);
document.getElementById('emg-modal-close').addEventListener('click', closeEmgModal);
emgOverlay.addEventListener('click', e => { if (e.target === emgOverlay) closeEmgModal(); });

// ── Init ──────────────────────────────────────────────────────────────────────
Promise.all([fetchStats(), fetchHistory(), fetchMetrics(), refreshTgState(), refreshNutState(), refreshEmgState()]);
setInterval(fetchStats,   10000);
setInterval(fetchMetrics, 60000);
setInterval(fetchHistory, 30000);
</script>
</body>
</html>"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=80)
