#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py  —  UAT Ops Chatbot  (Browser UI edition)
stdlib only. Python 3.6+. No pip. No LLM.

Deploy:
    scp server.py intents.py config.ini cpndev01@cpnuatap030:/home/cpndev01/scripts/Gokul/
    ssh cpndev01@cpnuatap030
    python3 /home/cpndev01/scripts/Gokul/server.py

Access from your laptop (SSH tunnel):
    ssh -L 8080:localhost:8080 cpndev01@cpnuatap030
    Open browser: http://localhost:8080

Or directly if the server IP is reachable in your network:
    http://cpnuatap030:8080
"""

import json
import os
import re
import sys
import socket
import tempfile
import threading
import subprocess
import configparser
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from intents import (
    classify_intent, is_all_request,
    extract_service, suggest_services, extract_env,
)
from vault_client import VaultClient
from api_checker  import (
    check_all_apis, check_flow_ready, find_api_by_text,
    get_apis, load_apis,
)


# ── Config ────────────────────────────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(_DIR, 'config.ini'), encoding='utf-8')
    general = dict(cfg['general']) if 'general' in cfg else {}
    # Also pull in [vault] section into general so VaultClient.from_config can read it
    if 'vault' in cfg:
        general.update({'vault.' + k: v for k, v in cfg['vault'].items()})
        # also flat keys for backward compat
        for k, v in cfg['vault'].items():
            general.setdefault(k, v)
    aliases = {}
    if 'service_aliases' in cfg:
        for k, v in cfg['service_aliases'].items():
            aliases[k.strip()] = v.strip()
    servers = {}
    for sec in cfg.sections():
        if sec.startswith('server:'):
            key = sec.split(':', 1)[1].strip()
            servers[key] = dict(cfg[sec])
            raw = servers[key].get('services', '')
            servers[key]['services'] = [s.strip() for s in raw.split(',') if s.strip()]
    return general, aliases, servers


GENERAL, SVC_ALIASES, SERVERS = load_config()

# ── Vault client (initialised once; shared across all requests) ───────────────
try:
    VAULT = VaultClient(
        vault_url    = GENERAL.get('url', ''),
        namespace    = GENERAL.get('namespace', ''),
        auth_method  = GENERAL.get('auth_method', 'approle'),
        role_id      = GENERAL.get('role_id', ''),
        secret_id    = GENERAL.get('secret_id', ''),
        static_token = GENERAL.get('token', ''),
        kv_backend   = GENERAL.get('kv_backend', 'kv'),
        kv_version   = int(GENERAL.get('kv_version', 1)),
        verify_ssl   = GENERAL.get('verify_ssl', 'true').lower() not in ('false', '0', 'no'),
    )
    VAULT_READY = True
except Exception as _ve:
    VAULT       = None
    VAULT_READY = False
    print('  [WARN] Vault not initialised: {0}'.format(_ve))

# Pre-load apis.json
load_apis()

_FLOWS_CONFIG = None

def load_flows():
    global _FLOWS_CONFIG
    flows_path = os.path.join(_DIR, 'flows.json')
    try:
        with open(flows_path, 'r') as f:
            _FLOWS_CONFIG = json.load(f)
    except Exception as ex:
        _FLOWS_CONFIG = {'flows': {}, '_load_error': str(ex)}
    return _FLOWS_CONFIG

def get_flows():
    if _FLOWS_CONFIG is None:
        load_flows()
    return _FLOWS_CONFIG.get('flows', {})

# Pre-load flows.json
load_flows()

# Global state for pending flow checks
PENDING_FLOW_CHECK = None




def get_all_services():
    seen = []
    for sc in SERVERS.values():
        for s in sc.get('services', []):
            if s not in seen:
                seen.append(s)
    return seen


ALL_SVCS = get_all_services()


def env_label(key):
    sc = SERVERS.get(key, {})
    return sc.get('aliases', key).split(',')[0].strip().upper()


def env_list():
    return [{'key': k, 'label': env_label(k), 'host': SERVERS[k]['host'],
             'services': SERVERS[k].get('services', [])} for k in SERVERS]


# ── Script execution ──────────────────────────────────────────────────────────

def _hostname():
    try:
        return socket.gethostname().lower()
    except Exception:
        return ''


def is_local(host):
    h = host.lower()
    me = _hostname()
    return h in me or me in h or h in ('localhost', '127.0.0.1')


def _run_local(cmd):
    """Popen + temp files — avoids PIPE deadlock with daemonising scripts (Python 3.6 issue)."""
    to = tempfile.NamedTemporaryFile(mode='w', suffix='.out', delete=False)
    te = tempfile.NamedTemporaryFile(mode='w', suffix='.err', delete=False)
    op, ep = to.name, te.name
    to.close()
    te.close()
    try:
        with open(op, 'w') as fout, open(ep, 'w') as ferr:
            p = subprocess.Popen(cmd, shell=True, stdout=fout, stderr=ferr,
                                 preexec_fn=os.setsid)
            try:
                p.wait(timeout=600)
            except subprocess.TimeoutExpired:
                p.kill()
                return '', 'Timed out after 600s', 1
        with open(op) as f:
            out = f.read()
        with open(ep) as f:
            err = f.read()
        return out, err, p.returncode
    except Exception as exc:
        return '', str(exc), 1
    finally:
        for path in (op, ep):
            try:
                os.unlink(path)
            except OSError:
                pass


def _run_remote(host, user, cmd, password=None):
    if password:
        # Zero-installation interactive SSH password login using pty (stdlib only)
        import pty
        import select
        
        ssh_cmd = ['ssh', '-o', 'BatchMode=no', '-o', 'ConnectTimeout=10', 
                   '-o', 'StrictHostKeyChecking=no', '{0}@{1}'.format(user, host), cmd]
        try:
            pid, fd = pty.fork()
            if pid == 0:
                os.execvp('ssh', ssh_cmd)
                
            output = []
            password_sent = False
            t0 = time.time()
            while True:
                if time.time() - t0 > 600:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
                    return '', 'SSH connection timed out (600s)', 1
                
                r, _, _ = select.select([fd], [], [], 0.5)
                if fd in r:
                    try:
                        data = os.read(fd, 1024)
                    except OSError:
                        break
                    if not data:
                        break
                    decoded = data.decode('utf-8', errors='replace')
                    output.append(decoded)
                    
                    if not password_sent and ('password:' in decoded.lower() or 'password :' in decoded.lower()):
                        os.write(fd, (password + '\n').encode('utf-8'))
                        password_sent = True
            try:
                _, status = os.waitpid(pid, 0)
                rc = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
            except OSError:
                rc = 0
                
            full_out = ''.join(output)
            clean_lines = []
            for line in full_out.splitlines():
                if 'password:' in line.lower() or password in line:
                    continue
                clean_lines.append(line)
            return '\n'.join(clean_lines), '', rc
        except Exception as exc:
            return '', 'PTY SSH Error: ' + str(exc), 1
    else:
        ssh = ('ssh -o BatchMode=yes -o ConnectTimeout=10 '
               '-o StrictHostKeyChecking=no {u}@{h} "{c}"').format(
            u=user, h=host, c=cmd.replace('"', '\\"'))
        try:
            r = subprocess.run(ssh, shell=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=600)
            return (r.stdout.decode('utf-8', 'replace'),
                    r.stderr.decode('utf-8', 'replace'),
                    r.returncode)
        except subprocess.TimeoutExpired:
            return '', 'SSH timed out (600s)', 1
        except Exception as exc:
            return '', str(exc), 1


def wrap_env_cmd(cmd):
    return (
        "source /etc/profile 2>/dev/null; "
        "source ~/.bash_profile 2>/dev/null; "
        "source ~/.bashrc 2>/dev/null; "
        "export PATH=$PATH:/usr/java/latest/bin:/usr/lib/jvm/java/bin:/usr/bin:/usr/local/bin; "
        + cmd
    )


def run_script(server_key, script_name, service):
    sc = SERVERS[server_key]
    host = sc.get('host.' + service, sc['host'])
    user = sc.get('user.' + service, sc.get('user', GENERAL.get('default_user', 'cpndev01')))
    scripts_dir = sc.get('scripts_dir.' + service, sc['scripts_dir'])
    password = sc.get('password.' + service, sc.get('password', None))
    
    if script_name == GENERAL.get('status_script', 'app_status.sh') and service in ('si', 'batch'):
        actual_script = sc.get('status_script.' + service)
        if not actual_script:
            cmd = 'jps'
        else:
            cmd = 'cd {d} && sh {s}'.format(d=scripts_dir, s=actual_script)
    else:
        actual_script = script_name
        if script_name == GENERAL.get('start_script', 'app_start.sh'):
            if service == 'si':
                actual_script = sc.get('start_script.si', 'startSI')
            elif service == 'batch':
                actual_script = sc.get('start_script.batch', 'startNAPAll.sh')
            else:
                actual_script = sc.get('start_script.' + service, 'app_start.sh')
        elif script_name == GENERAL.get('stop_script', 'app_stop.sh'):
            if service == 'si':
                actual_script = sc.get('stop_script.si', 'stopSI')
            elif service == 'batch':
                actual_script = sc.get('stop_script.batch', 'stopNAPAll.sh')
            else:
                actual_script = sc.get('stop_script.' + service, 'app_stop.sh')
        cmd = 'cd {d} && sh {s} {svc}'.format(d=scripts_dir, s=actual_script, svc=service)
        
    wrapped_cmd = wrap_env_cmd(cmd)
    return _run_local(wrapped_cmd) if is_local(host) else _run_remote(host, user, wrapped_cmd, password)


def parse_status(out):
    clean = out.strip().lower()
    if 'not running' in clean:
        return 'DOWN'
    if 'up' in clean or 'running' in clean:
        return 'UP'
    if 'down' in clean or 'stopped' in clean or 'pid file not found' in clean:
        return 'DOWN'
    return 'DOWN'


def get_pid(out):
    m = re.search(r'(\d{3,6}) is running', out)
    return m.group(1) if m else '-'


# ── Service actions ───────────────────────────────────────────────────────────

def svc_status(server_key, service):
    out, err, rc = run_script(server_key, GENERAL.get('status_script', 'app_status.sh'), service)
    if rc != 0 and err.strip():
        return {'state': 'ERROR', 'pid': '-', 'err': err.strip()[:200]}
    
    sc = SERVERS[server_key]
    if service == 'si':
        actual_script = sc.get('status_script.si')
        if actual_script:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            state_val = lines[-1] if lines else 'UNKNOWN'
            is_up = 'running' in state_val.lower() and 'not' not in state_val.lower()
            state_str = 'UP' if is_up else 'DOWN'
            return {'state': '{0} ({1})'.format(state_str, state_val), 'pid': '-'}
        else:
            count = out.count('MasterThread')
            state = 'UP' if count >= 2 else 'DOWN'
            return {'state': '{0} ({1}/2 MasterThread)'.format(state, count), 'pid': '-'}
            
    elif service == 'batch':
        actual_script = sc.get('status_script.batch')
        if actual_script:
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            state_val = lines[-1] if lines else 'UNKNOWN'
            is_up = 'running' in state_val.lower() and 'not' not in state_val.lower()
            state_str = 'UP' if is_up else 'DOWN'
            return {'state': '{0} ({1})'.format(state_str, state_val), 'pid': '-'}
        else:
            count = out.count('NCSAsynchronousProcessor')
            state = 'UP' if count >= 5 else 'DOWN'
            return {'state': '{0} ({1}/5 NAP)'.format(state, count), 'pid': '-'}
        
    state = parse_status(out)
    return {'state': state, 'pid': get_pid(out) if state == 'UP' else '-'}


def svc_status_all(server_key):
    results = []
    for svc in SERVERS[server_key].get('services', []):
        r = svc_status(server_key, svc)
        r['service'] = svc
        results.append(r)
    return results


def log_activity(env, service, action, status, stdout="", stderr=""):
    import datetime
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_line = (
        "======================================================================\n"
        "Timestamp   : {0}\n"
        "Environment : {1}\n"
        "Service     : {2}\n"
        "Action      : {3}\n"
        "Status      : {4}\n"
    ).format(timestamp, env, service, action, status)
    
    if stdout.strip():
        log_line += "Stdout      :\n{0}\n".format(stdout.strip())
    if stderr.strip():
        log_line += "Stderr      :\n{0}\n".format(stderr.strip())
        
    log_line += "======================================================================\n\n"
    
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'activity.log')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(log_line)
    except Exception as exc:
        sys.stderr.write("Failed to write activity.log: " + str(exc) + "\n")


def log_event(level, module, message, details=None):
    """
    Production-grade industry logging for Ops Chatbot.
    Logs [INFO], [ERROR], [DEBUG] events to activity.log and server.log for host debugging.
    """
    import datetime
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_line = "[{ts}] [{lvl}] [{mod}] {msg}".format(ts=timestamp, lvl=level.upper(), mod=module, msg=message)
    if details:
        log_line += "\n  Details: " + str(details).replace("\n", "\n  ")
    log_line += "\n"
    
    log_dir = os.path.dirname(os.path.abspath(__file__))
    for fname in ('activity.log', 'server.log'):
        try:
            with open(os.path.join(log_dir, fname), 'a', encoding='utf-8') as f:
                f.write(log_line)
        except Exception:
            pass


def wait_for_status_script(server_key, service, target_state, timeout=45):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        status = svc_status(server_key, service)
        state = status.get('state', 'UNKNOWN')
        if target_state == 'UP' and state.startswith('UP'):
            return status
        elif target_state == 'DOWN' and (state.startswith('DOWN') or state == 'UNKNOWN'):
            return status
        time.sleep(1.5)
    return svc_status(server_key, service)


def svc_start(server_key, service):
    out, err, rc = run_script(server_key, GENERAL.get('start_script', 'app_start.sh'), service)
    status = wait_for_status_script(server_key, service, 'UP', timeout=45)
    live_state = status.get('state', 'UNKNOWN')
    
    is_up = live_state.startswith('UP')
    log_activity(env_label(server_key), service, 'START', live_state, out, err)

    if is_up:
        return {'ok': True, 'msg': '✅ <b>{0}</b> started successfully! Status: <b>{1}</b>'.format(service, live_state)}
    else:
        return {'ok': False, 'msg': '❌ <b>{0}</b> failed to start. Status is still <b>{1}</b>. <span style="font-size:11px;color:var(--muted)">Check activity.log for stdout/stderr logs.</span>'.format(service, live_state)}


def svc_stop(server_key, service):
    out, err, rc = run_script(server_key, GENERAL.get('stop_script', 'app_stop.sh'), service)
    status = wait_for_status_script(server_key, service, 'DOWN', timeout=45)
    live_state = status.get('state', 'UNKNOWN')
    
    is_down = live_state.startswith('DOWN') or live_state == 'UNKNOWN'
    log_activity(env_label(server_key), service, 'STOP', live_state, out, err)

    if is_down:
        return {'ok': True, 'msg': '✅ <b>{0}</b> stopped successfully! Status: <b>{1}</b>'.format(service, live_state)}
    else:
        return {'ok': False, 'msg': '❌ <b>{0}</b> failed to stop. Status is still <b>{1}</b>. <span style="font-size:11px;color:var(--muted)">Check activity.log for stdout/stderr logs.</span>'.format(service, live_state)}


def svc_restart(server_key, service):
    out_stop, err_stop, rc_stop = run_script(server_key, GENERAL.get('stop_script', 'app_stop.sh'), service)
    status_stop = wait_for_status_script(server_key, service, 'DOWN', timeout=45)
    live_state_stop = status_stop.get('state', 'UNKNOWN')
    log_activity(env_label(server_key), service, 'RESTART_STOP', live_state_stop, out_stop, err_stop)
    
    out_start, err_start, rc_start = run_script(server_key, GENERAL.get('start_script', 'app_start.sh'), service)
    status_start = wait_for_status_script(server_key, service, 'UP', timeout=45)
    live_state_start = status_start.get('state', 'UNKNOWN')
    log_activity(env_label(server_key), service, 'RESTART_START', live_state_start, out_start, err_start)

    is_up = live_state_start.startswith('UP')
    if is_up:
        return {'ok': True, 'msg': '✅ <b>{0}</b> restarted successfully! Status: <b>{1}</b>'.format(service, live_state_start)}
    else:
        return {'ok': False, 'msg': '❌ <b>{0}</b> failed to restart. Status: <b>{1}</b>. <span style="font-size:11px;color:var(--muted)">Check activity.log for details.</span>'.format(service, live_state_start)}


# ── Chat response builders ────────────────────────────────────────────────────

def _text(html):
    return {'type': 'text', 'html': html}


def _choice(question, options):
    return {'type': 'choice', 'html': '<p>{0}</p>'.format(question), 'options': options}


def _status_table(results, server_key):
    label = env_label(server_key)
    up = sum(1 for r in results if r.get('state') == 'UP')
    rows = ''
    for r in results:
        state = r.get('state', 'UNKNOWN')
        is_up = state == 'UP'
        color = '#3fb950' if is_up else '#f85149'
        dot = '&#9679;'
        rows += ('<tr><td><b>{svc}</b></td>'
                 '<td><span style="color:{col}">{dot}</span> {state}</td>'
                 '<td>{pid}</td>'
                 '<td class="action-cell">'
                 '<button onclick="quickAction(\'start\',\'{svc}\',\'{env}\')">&#9654;</button>'
                 '<button onclick="quickAction(\'stop\',\'{svc}\',\'{env}\')">&#9632;</button>'
                 '<button onclick="quickAction(\'restart\',\'{svc}\',\'{env}\')">&#8635;</button>'
                 '</td></tr>').format(
            svc=r['service'], col=color, dot=dot, state=state, pid=r.get('pid', '-'), env=server_key)
    summary_color = '#3fb950' if up == len(results) else ('#d29922' if up > 0 else '#f85149')
    html = ('<p>&#128507; <b>[{env}]</b> &mdash; '
            '<span style="color:{sc}">{up}/{total} services running</span></p>'
            '<table class="t"><tr><th>Service</th><th>Status</th>'
            '<th>PID</th><th>Actions</th></tr>{rows}</table>').format(
        env=label, sc=summary_color, up=up, total=len(results), rows=rows)
    return {'type': 'table', 'html': html}


def _single_status(server_key, service, r):
    state = r.get('state', 'UNKNOWN')
    is_up = state == 'UP'
    color = '#3fb950' if is_up else '#f85149'
    dot = '&#9679;'
    label = env_label(server_key)
    html = ('<table class="t"><tr><th>Service</th><th>Env</th>'
            '<th>Status</th><th>PID</th><th>Actions</th></tr>'
            '<tr><td><b>{svc}</b></td><td>[{env}]</td>'
            '<td><span style="color:{col}">{dot}</span> {state}</td>'
            '<td>{pid}</td>'
            '<td class="action-cell">'
            '<button onclick="quickAction(\'start\',\'{svc}\',\'{sk}\')">&#9654;</button>'
            '<button onclick="quickAction(\'stop\',\'{svc}\',\'{sk}\')">&#9632;</button>'
            '<button onclick="quickAction(\'restart\',\'{svc}\',\'{sk}\')">&#8635;</button>'
            '</td></tr></table>').format(
        svc=service, env=label, col=color, dot=dot, state=state, pid=r.get('pid', '-'), sk=server_key)
    return {'type': 'table', 'html': html}


def _help_html():
    return ('<table class="t"><tr><th>Type this</th><th>Does this</th></tr>'
            '<tr><td>is EAI up?</td><td>Check EAI status</td></tr>'
            '<tr><td>check all services</td><td>Check all on current env</td></tr>'
            '<tr><td>check all on sit</td><td>Check all on SIT/027</td></tr>'
            '<tr><td>restart cans</td><td>Stop then start cans</td></tr>'
            '<tr><td>start wmq</td><td>Start wmq-file-Integrator</td></tr>'
            '<tr><td>stop EAI</td><td>Stop EAI</td></tr>'
            '<tr><td>list services</td><td>List services on current env</td></tr>'
            '</table>'
            '<p style="margin-top:8px"><b>Aliases:</b> rmq&rarr;rmq-producer &nbsp;'
            'wmq&rarr;wmq-file-Integrator &nbsp; eai&rarr;EAI &nbsp; can&rarr;cans</p>'
            '<p><b>Envs:</b> uat/030 &nbsp; jpp/jprepro/036 &nbsp; sit/dev/jsit/027</p>')


# ── Message processor ─────────────────────────────────────────────────────────

def _exec(intent, service, server_key):
    """Execute a resolved intent+service+server and return a response dict."""
    if server_key not in SERVERS:
        return _text('Unknown environment key: {0}'.format(server_key))
    label = env_label(server_key)

    if intent == 'status':
        r = svc_status(server_key, service)
        return _single_status(server_key, service, r)

    if intent == 'start':
        r = svc_start(server_key, service)
        icon = '&#9989;' if r['ok'] else '&#10060;'
        return _text('{icon} <b>{svc}</b> on [{env}]: {msg}'.format(
            icon=icon, svc=service, env=label, msg=r['msg']))

    if intent == 'stop':
        r = svc_stop(server_key, service)
        icon = '&#9989;' if r['ok'] else '&#10060;'
        return _text('{icon} <b>{svc}</b> on [{env}]: {msg}'.format(
            icon=icon, svc=service, env=label, msg=r['msg']))

    if intent == 'restart':
        r = svc_restart(server_key, service)
        icon = '&#9989;' if r['ok'] else '&#10060;'
        return _text('{icon} Restarted <b>{svc}</b> on [{env}]: {msg}'.format(
            icon=icon, svc=service, env=label, msg=r['msg']))

    return _text('Unknown intent: {0}'.format(intent))


def _api_state_icon(state):
    if state == 'UP':
        return '&#9989;'    # ✅
    if state in ('AUTH_ERROR', 'CONFIG_ERROR'):
        return '&#9888;'    # ⚠️
    if state == 'SKIP':
        return '&#8212;'    # —
    return '&#10060;'       # ❌


def _api_state_color(state):
    if state == 'UP':
        return '#3fb950'
    if state in ('AUTH_ERROR', 'CONFIG_ERROR'):
        return '#d29922'
    return '#f85149'


def _vault_not_ready_msg():
    return _text(
        '&#128274; <b>Vault not configured.</b><br>'
        'Open <code>config.ini</code> and fill in:<br>'
        '<code>role_id  = &lt;your AppRole role-id&gt;</code><br>'
        '<code>secret_id = &lt;your AppRole secret-id&gt;</code>'
    )


def _render_api_table(results, label):
    up    = sum(1 for r in results if r.get('state') == 'UP')
    total = len(results)
    sc    = '#3fb950' if up == total else ('#d29922' if up > 0 else '#f85149')

    rows = ''
    for r in results:
        state   = r.get('state', 'UNKNOWN')
        icon    = _api_state_icon(state)
        color   = _api_state_color(state)
        rt      = '{0}ms'.format(r.get('response_time_ms', 0))
        contact = ''
        if state not in ('UP', 'SKIP') and r.get('contact'):
            contact = '<br><span style="color:#d29922;font-size:11px">&#9888; {0}</span>'.format(
                r['contact'])
        if r.get('error') and state not in ('UP',):
            contact += '<br><span style="color:#8b949e;font-size:11px">{0}</span>'.format(
                r['error'][:80])

        rows += ('<tr><td><b>{name}</b></td>'
                 '<td><span style="color:{col}">{icon}</span> {state}{contact}</td>'
                 '<td>{rt}</td>'
                 '<td>{team}</td></tr>').format(
            name=r.get('name', ''), col=color, icon=icon, state=state,
            contact=contact, rt=rt, team=r.get('team', ''))

    html = ('<p>&#127760; <b>[{env}]</b> APIs &mdash; '
            '<span style="color:{sc}">{up}/{total} reachable</span></p>'
            '<table class="t">'
            '<tr><th>API</th><th>Status</th><th>Time</th><th>Team</th></tr>'
            '{rows}</table>').format(
        env=label, sc=sc, up=up, total=total, rows=rows)
    return {'type': 'table', 'html': html}


def _all_apis_response(current_env):
    if not VAULT_READY or VAULT is None:
        return _vault_not_ready_msg()
    sc      = SERVERS.get(current_env, {})
    results = check_all_apis(VAULT, sc)
    return _render_api_table(results, env_label(current_env))


def extract_flow(text, flows):
    lower = text.lower()
    for key, flow_cfg in flows.items():
        if key in lower or flow_cfg.get('name', '').lower() in lower:
            return key
    return None


def _flow_check_response(flow_key, env_key):
    flows = get_flows()
    flow = flows.get(flow_key)
    if not flow:
        return _text('Unknown flow: {0}'.format(flow_key))

    label = env_label(env_key)
    server_cfg = SERVERS[env_key]
    from api_checker import get_env_dns_label

    # 1. Services check
    services = flow.get('services', [])
    svc_results = []
    svc_down = []
    for svc in services:
        r = svc_status(env_key, svc)
        state = r.get('state', 'UNKNOWN')
        pid = r.get('pid', '-')

        # Actuator check for wmq-file-integrator if process is running
        actuator_state = None
        actuator_err = None
        if svc.lower() == 'wmq-file-integrator' and state == 'UP':
            from api_checker import check_api
            api_cfg = {
                'name': 'wmq-file-integrator Actuator',
                'type': 'actuator',
                'health_url_template': 'https://capstone-mercury-{env_label}.fr.world.socgen:8900/actuator/health',
                'timeout_sec': 5
            }
            act_res = check_api(api_cfg, {}, VAULT._ssl if VAULT else None, server_cfg)
            if act_res and isinstance(act_res, dict):
                actuator_state = act_res.get('state')
                if actuator_state != 'UP':
                    actuator_err = act_res.get('error')

        is_up = (state == 'UP')
        if svc.lower() == 'wmq-file-integrator' and actuator_state and actuator_state != 'UP':
            is_up = False
            state = 'UP (Process) | DOWN (Actuator)'

        if is_up:
            svc_results.append((svc, state, pid, None))
        else:
            svc_down.append(svc)
            err_msg = actuator_err if actuator_err else 'Process not running'
            svc_results.append((svc, state, pid, err_msg))

    # 2. APIs check
    api_names = flow.get('apis', [])
    api_results = []
    api_down = []

    if api_names:
        if not VAULT_READY or VAULT is None:
            log_event('ERROR', 'VAULT', 'Vault not ready or failed to connect when checking flow "{0}" in [{1}]'.format(flow_key, label))
            # Fallback for TCP checks if Vault not ready
            from api_checker import get_apis, check_api
            apis = [a for a in get_apis() if a.get('name') in api_names]
            for api_cfg in apis:
                if api_cfg.get('type') == 'tcp':
                    r = check_api(api_cfg, {}, None, server_cfg)
                else:
                    r = {
                        'name': api_cfg.get('name'),
                        'state': 'ERROR',
                        'error': 'Vault not configured — skip API health check'
                    }
                api_results.append(r)
        else:
            # Fetch secrets
            secret_path = server_cfg.get('vault_secret_path', '')
            if flow_key == 'bloomberg':
                secret_path = 'kv/cans-' + get_env_dns_label(server_cfg)

            try:
                secrets = VAULT.get_secret(secret_path) if secret_path else {}
            except Exception as exc:
                secrets = {}
                import traceback
                log_event('ERROR', 'VAULT', 'Failed to fetch secrets from Vault at path "{0}" for flow "{1}" in [{2}]'.format(secret_path, flow_key, label), traceback.format_exc())

            from api_checker import check_api, get_apis
            import threading

            flow_apis = [a for a in get_apis() if a.get('name') in api_names]
            api_results = [None] * len(flow_apis)
            threads = []

            def worker(index, api_cfg):
                try:
                    api_results[index] = check_api(api_cfg, secrets, VAULT._ssl, server_cfg)
                except Exception as exc:
                    api_results[index] = {
                        'name': api_cfg.get('name'),
                        'state': 'ERROR',
                        'error': str(exc)[:200]
                    }

            for i, api_cfg in enumerate(flow_apis):
                t = threading.Thread(target=worker, args=(i, api_cfg))
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

        for r in api_results:
            if r.get('state') != 'UP':
                api_down.append(r.get('name'))

    # 3. Overall verdict
    issues = len(svc_down) + len(api_down)
    if issues == 0:
        verdict_color = '#3fb950'
        verdict = '&#10003; <b>{0} is READY to test in {1}! All components UP.</b>'.format(flow.get('name', 'Flow'), label)
    else:
        verdict_color = '#f85149'
        parts = []
        if svc_down:
            parts.append('{0} service(s) down ({1})'.format(len(svc_down), ', '.join(svc_down)))
        if api_down:
            parts.append('{0} API(s) down ({1})'.format(len(api_down), ', '.join(api_down)))
        verdict = '&#9888; <b>{0} NOT ready in {1}</b> &mdash; {2}'.format(
            flow.get('name', 'Flow'), label, ' | '.join(parts))

    # Format HTML response
    svc_rows = ''
    for name, state, pid, err in svc_results:
        is_up = (state == 'UP')
        color = '#3fb950' if is_up else '#f85149'
        dot = '<span style="color:{c}">&#9679;</span>'.format(c=color)
        err_str = '<br><span style="color:#d29922;font-size:11px">&#9888; {0}</span>'.format(err) if err else ''
        svc_rows += '<tr><td>{dot}</td><td><b>{n}</b></td><td>{s}{e}</td><td>{p}</td></tr>'.format(
            dot=dot, n=name, s=state, e=err_str, p=pid)

    api_rows = ''
    for r in api_results:
        state = r.get('state', 'UNKNOWN')
        is_up = (state == 'UP')
        color = '#3fb950' if is_up else '#f85149'
        dot = '<span style="color:{c}">&#9679;</span>'.format(c=color)
        rt = '{0}ms'.format(r.get('response_time_ms', 0)) if r.get('response_time_ms') else '—'

        contact = ''
        if not is_up and r.get('contact'):
            contact = '<br><span style="color:#d29922;font-size:11px">&#9888; {0}</span>'.format(r['contact'])
        if r.get('error') and not is_up:
            contact += '<br><span style="color:#8b949e;font-size:11px">{0}</span>'.format(r['error'][:80])

        api_rows += '<tr><td>{dot}</td><td><b>{name}</b></td><td>{state}{contact}</td><td>{rt}</td></tr>'.format(
            dot=dot, name=r.get('name'), state=state, contact=contact, rt=rt)

    if not api_rows:
        api_rows = '<tr><td colspan="4" style="color:#8b949e">No APIs checked for this flow</td></tr>'

    details_html = ('<details style="margin-top:8px;cursor:pointer"><summary style="color:var(--blue);font-size:13px">View Component Details</summary>'
                    '<p style="font-size:12px;color:#8b949e;margin-top:8px">SERVICES</p>'
                    '<table class="t">'
                    '<tr><th></th><th>Service</th><th>Status</th><th>PID</th></tr>'
                    '{svc_rows}</table>'
                    '<p style="font-size:12px;color:#8b949e;margin-top:8px">APIS</p>'
                    '<table class="t">'
                    '<tr><th></th><th>API</th><th>Status</th><th>Response</th></tr>'
                    '{api_rows}</table></details>').format(svc_rows=svc_rows, api_rows=api_rows)

    html = ('<div style="border-left:4px solid {vc};padding-left:12px;margin:8px 0">'
            '<p style="font-size:14px;font-weight:bold;color:{vc}">{verdict}</p>'
            '{details}</div>').format(vc=verdict_color, verdict=verdict, details=details_html)

    return {'type': 'table', 'html': html}



def _flow_ready_response(current_env):
    """Run services + core flow APIs and report combined pass/fail."""
    label    = env_label(current_env)
    sc_cfg   = SERVERS.get(current_env, {})
    services = sc_cfg.get('services', [])

    # Services check
    svc_rows  = ''
    svc_up    = 0
    svc_down  = []
    for svc in services:
        r     = svc_status(current_env, svc)
        state = r.get('state', 'UNKNOWN')
        pid   = r.get('pid', '-')
        is_up = state == 'UP'
        if is_up:
            svc_up += 1
        else:
            svc_down.append(svc)
        color = '#3fb950' if is_up else '#f85149'
        dot   = '<span style="color:{c}">&#9679;</span>'.format(c=color)
        svc_rows += '<tr><td>{dot}</td><td><b>{s}</b></td><td>{state}</td><td>{pid}</td></tr>'.format(
            dot=dot, s=svc, state=state, pid=pid)

    # APIs check (flow-critical only)
    api_rows   = ''
    api_up     = 0
    api_down   = []
    api_errors = []

    if not VAULT_READY or VAULT is None:
        api_rows = ('<tr><td colspan="4" style="color:#d29922">'
                    '&#9888; Vault not configured — API checks skipped</td></tr>')
    else:
        from api_checker import check_flow_ready as _cfr
        api_results = _cfr(VAULT, sc_cfg)
        for r in api_results:
            state   = r.get('state', 'UNKNOWN')
            is_up   = state == 'UP'
            if is_up:
                api_up += 1
            else:
                api_down.append(r.get('name'))
                if r.get('contact'):
                    api_errors.append('{0}: {1}'.format(r.get('name'), r.get('contact')))
            color = '#3fb950' if is_up else '#f85149'
            dot   = '<span style="color:{c}">&#9679;</span>'.format(c=color)
            rt    = '{0}ms'.format(r.get('response_time_ms', 0))
            team  = r.get('team', '')
            contact_note = ''
            if not is_up and r.get('contact'):
                contact_note = '<br><span style="color:#d29922;font-size:11px">&#9888; {0}</span>'.format(r['contact'])
            api_rows += ('<tr><td>{dot}</td><td><b>{name}</b></td>'
                         '<td>{state}{contact}</td><td>{rt}</td></tr>').format(
                dot=dot, name=r.get('name', ''), state=state,
                contact=contact_note, rt=rt)

    # Overall verdict
    issues = len(svc_down) + len(api_down)
    if issues == 0:
        verdict_color = '#3fb950'
        verdict       = '&#10003; <b>Flow is READY to test!</b>'
    else:
        verdict_color = '#f85149'
        parts = []
        if svc_down:
            parts.append('{0} service(s) down: {1}'.format(len(svc_down), ', '.join(svc_down)))
        if api_down:
            parts.append('{0} API(s) unreachable: {1}'.format(len(api_down), ', '.join(api_down)))
        verdict = '&#9888; <b>Flow NOT ready</b> &mdash; ' + ' | '.join(parts)

    html = ('<p>&#128269; <b>Full flow check on [{env}]</b></p>'
            '<p style="font-size:12px;color:#8b949e">SERVICES</p>'
            '<table class="t">'
            '<tr><th></th><th>Service</th><th>Status</th><th>PID</th></tr>'
            '{svc_rows}</table>'
            '<p style="font-size:12px;color:#8b949e;margin-top:8px">APIS (flow-critical)</p>'
            '<table class="t">'
            '<tr><th></th><th>API</th><th>Status</th><th>Response</th></tr>'
            '{api_rows}</table>'
            '<p style="margin-top:10px;color:{vc}">{verdict}</p>').format(
        env=label, svc_rows=svc_rows, api_rows=api_rows,
        vc=verdict_color, verdict=verdict)

    return {'type': 'table', 'html': html}


def process_message(text, current_env):
    """
    Parse a chat message and return a response dict.
    Special prefix __action__:intent:service:env → skip parsing, run directly.
    This is used by disambiguation option buttons.
    """
    # Direct action from a disambiguation button click
    if text.startswith('__action__:'):
        parts = text.split(':', 3)
        if len(parts) == 4:
            return _exec(parts[1], parts[2], parts[3])
        return _text('Bad action format.')

    lower = text.lower().strip()

    # ── Multi-turn pending flow check environment confirmation ───────────────
    global PENDING_FLOW_CHECK
    if PENDING_FLOW_CHECK:
        env_key = extract_env(lower, SERVERS)
        if env_key:
            flow_key = PENDING_FLOW_CHECK['flow']
            PENDING_FLOW_CHECK = None
            return _flow_check_response(flow_key, env_key)
        else:
            if lower in ('exit', 'quit', 'cancel', 'c'):
                PENDING_FLOW_CHECK = None
                return _text('Cancelled flow check.')
            return _text('Please specify a valid environment (<b>juat</b>, <b>jpreprod</b>, or <b>jsit</b>) to check the flow, or type <b>cancel</b>.')

    # ── Meta ─────────────────────────────────────────────────────────────────
    if lower in ('help', '?', 'commands', 'h'):
        return _text(_help_html())

    if re.search(r'\blist\b.*(service|app)', lower) or lower in ('services', 'apps'):
        sc = SERVERS.get(current_env, {})
        items = ''.join('<li>{0}</li>'.format(s) for s in sc.get('services', []))
        return _text('<b>Services on [{0}]:</b><ul style="margin-left:16px">{1}</ul>'.format(
            env_label(current_env), items))

    # ── Flow check: e.g. "I am testing DTCC flow" ────────────────────────────
    flow_key = extract_flow(lower, get_flows())
    if flow_key:
        target_env = extract_env(lower, SERVERS)
        if target_env:
            return _flow_check_response(flow_key, target_env)
        else:
            PENDING_FLOW_CHECK = {'flow': flow_key}
            return _text('Which environment are you checking this flow on? (Type: <b>juat</b>, <b>jpreprod</b>, or <b>jsit</b>)')

    # ── "Is the flow ready?" — checks services + key APIs together ────────────
    _FLOW_TRIGGERS = ('flow ready', 'is the flow', 'ready to test', 'flow ok',
                      'can i test', 'everything ready', 'ready for testing',
                      'nuvo flow ready', 'is flow')
    if any(t in lower for t in _FLOW_TRIGGERS):
        return _flow_ready_response(current_env)

    # ── API health — "check all apis", "is galaxy up?" etc. ──────────────────
    _API_ALL_TRIGGERS = ('check all apis', 'all apis', 'api status', 'api health',
                         'check apis', 'all api')
    if any(t in lower for t in _API_ALL_TRIGGERS):
        return _all_apis_response(current_env)

    # Single API check: "is galaxy up?", "is nuvo down?"
    if not VAULT_READY or VAULT is None:
        pass  # fall through to service check
    else:
        matched_api = find_api_by_text(text)
        if matched_api and not extract_service(text, SERVERS.get(current_env, {}).get('services', []), SVC_ALIASES):
            # Looks like an API query, not a service query
            return _single_api_response(matched_api, current_env)

    # ── Intent + target env ───────────────────────────────────────────────────
    intent = classify_intent(text)
    is_all = is_all_request(text)
    target = extract_env(text, SERVERS) or current_env
    sc = SERVERS.get(target, {})

    # ── All services ──────────────────────────────────────────────────────────
    if is_all:
        if intent == 'status':
            results = svc_status_all(target)
            return _status_table(results, target)

        if intent == 'start':
            lines = []
            for svc in sc.get('services', []):
                r = svc_start(target, svc)
                icon = '&#9989;' if r['ok'] else '&#10060;'
                lines.append('{0} {1}: {2}'.format(icon, svc, r['msg']))
            return _text('<br>'.join(lines))

        if intent in ('stop', 'restart'):
            lines = []
            fn = svc_restart if intent == 'restart' else svc_stop
            for svc in sc.get('services', []):
                r = fn(target, svc)
                icon = '&#9989;' if r['ok'] else '&#10060;'
                lines.append('{0} {1}: {2}'.format(icon, svc, r['msg']))
            return _text('<br>'.join(lines))

    # ── Single service ────────────────────────────────────────────────────────
    service = extract_service(text, sc.get('services', []), SVC_ALIASES)

    if not service:
        suggestions = suggest_services(text, ALL_SVCS, SVC_ALIASES)

        if not suggestions:
            return _text(
                "I didn't catch a service name. Try: <i>is EAI up</i>, <i>restart cans</i>, <i>check all</i><br>"
                'Known services: <b>{0}</b>'.format(', '.join(ALL_SVCS)))

        if len(suggestions) == 1:
            service = suggestions[0]
            resp = _exec(intent, service, target)
            resp['html'] = ('<p style="color:#8b949e;font-size:12px">&#128269; '
                            'Resolved to <b>{0}</b></p>'.format(service)) + resp.get('html', '')
            return resp

        # Multiple → show disambiguation buttons
        opts = [{'label': s,
                 'value': '__action__:{i}:{s}:{e}'.format(i=intent, s=s, e=target)}
                for s in suggestions]
        return _choice('Multiple matches &mdash; which service did you mean?', opts)

    # Service found — check it exists on target env
    if service not in sc.get('services', []):
        cands = [k for k, s in SERVERS.items() if service in s.get('services', [])]
        if not cands:
            return _text("'{0}' not found on any configured server.".format(service))
        if len(cands) == 1:
            target = cands[0]
        else:
            opts = [{'label': '[{0}] {1}'.format(env_label(k), SERVERS[k]['host']),
                     'value': '__action__:{i}:{s}:{e}'.format(i=intent, s=service, e=k)}
                    for k in cands]
            return _choice('Which environment for <b>{0}</b>?'.format(service), opts)

    return _exec(intent, service, target)


# ── HTML page (embedded) ──────────────────────────────────────────────────────

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UAT Ops Chatbot</title>
<style>
:root {
  --bg:     #0d1117;
  --panel:  #161b22;
  --border: #30363d;
  --text:   #c9d1d9;
  --muted:  #8b949e;
  --blue:   #58a6ff;
  --green:  #3fb950;
  --red:    #f85149;
  --orange: #d29922;
  --hover:  #1f2937;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Courier New', Courier, monospace;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ── Header ── */
header {
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  padding: 10px 18px;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-shrink: 0;
}
header h1 {
  font-size: 15px;
  color: var(--blue);
  letter-spacing: 0.5px;
}
header .sep { color: var(--border); }
.env-bar { display: flex; gap: 6px; }
.env-btn {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 4px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
  transition: all 0.15s;
}
.env-btn:hover { border-color: var(--blue); color: var(--blue); }
.env-btn.active { background: var(--blue); color: #fff; border-color: var(--blue); }
.conn-info { font-size: 11px; color: var(--muted); margin-left: auto; }

/* ── Main layout ── */
.main {
  display: flex;
  flex: 1;
  overflow: hidden;
  gap: 0;
}

/* ── Chat panel ── */
.chat-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border);
  min-width: 0;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.messages::-webkit-scrollbar { width: 6px; }
.messages::-webkit-scrollbar-track { background: transparent; }
.messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

.bubble-row { display: flex; flex-direction: column; }
.bubble-row.user { align-items: flex-end; }
.bubble-row.bot  { align-items: flex-start; }

.bubble {
  max-width: 82%;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.55;
}
.bubble.user {
  background: #1d3557;
  border-bottom-right-radius: 2px;
  color: #a8d8f0;
}
.bubble.bot {
  background: var(--panel);
  border: 1px solid var(--border);
  border-bottom-left-radius: 2px;
}
.bubble.bot.thinking { color: var(--muted); font-style: italic; }

.options-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.opt-btn {
  background: var(--bg);
  border: 1px solid var(--blue);
  color: var(--blue);
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  font-family: inherit;
}
.opt-btn:hover { background: var(--blue); color: #fff; }

/* ── Tables inside chat bubbles ── */
.t { border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 2px; }
.t th, .t td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; }
.t th { background: var(--hover); color: var(--muted); font-weight: normal; }
.action-cell button {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  cursor: pointer;
  border-radius: 3px;
  padding: 2px 5px;
  font-size: 11px;
  margin-right: 2px;
}
.action-cell button:hover { border-color: var(--blue); color: var(--blue); }
ul { padding-left: 16px; }

/* ── Input bar ── */
.input-bar {
  border-top: 1px solid var(--border);
  padding: 10px 12px;
  background: var(--panel);
  flex-shrink: 0;
}
.quick-btns {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 8px;
}
.qbtn {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 4px;
  padding: 3px 9px;
  font-size: 11px;
  cursor: pointer;
  font-family: inherit;
}
.qbtn:hover { border-color: var(--blue); color: var(--blue); }
.input-row { display: flex; gap: 8px; }
#msg-input {
  flex: 1;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text);
  padding: 7px 10px;
  font-size: 13px;
  font-family: inherit;
  outline: none;
}
#msg-input:focus { border-color: var(--blue); }
#msg-input::placeholder { color: var(--muted); }
#send-btn {
  background: var(--blue);
  border: none;
  color: #fff;
  border-radius: 4px;
  padding: 7px 18px;
  font-size: 13px;
  cursor: pointer;
  font-family: inherit;
}
#send-btn:hover { background: #4096e8; }
#send-btn:disabled { background: var(--border); cursor: default; }

/* ── Status dashboard ── */
.dash-panel {
  width: 400px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--panel);
}
.dash-header {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}
.dash-title { font-size: 13px; color: var(--blue); flex: 1; }
#refresh-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 4px;
  padding: 3px 10px;
  font-size: 11px;
  cursor: pointer;
  font-family: inherit;
}
#refresh-btn:hover { border-color: var(--blue); color: var(--blue); }
.dash-body { flex: 1; overflow-y: auto; padding: 10px 12px; }
.dash-env-label {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 6px;
}
.dash-table { border-collapse: collapse; width: 100%; font-size: 12px; }
.dash-table th, .dash-table td {
  border: 1px solid var(--border);
  padding: 5px 8px;
  text-align: left;
}
.dash-table th { background: var(--hover); color: var(--muted); font-weight: normal; }
.dash-table .action-cell button {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  cursor: pointer;
  border-radius: 3px;
  padding: 2px 4px;
  font-size: 10px;
  margin-right: 2px;
}
.dash-table .action-cell button:hover { border-color: var(--blue); color: var(--blue); }
.last-updated { font-size: 10px; color: var(--muted); margin-top: 8px; text-align: right; }
.dot-up   { color: var(--green); }
.dot-down { color: var(--red); }
.dot-stale { color: var(--orange); }
.spinning { display: inline-block; animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<header>
  <h1>&#128187; UAT Ops Chatbot</h1>
  <span class="sep">|</span>
  <div class="env-bar" id="env-bar"><!-- populated by JS --></div>
  <span class="conn-info" id="conn-info"></span>
</header>

<div class="main">

  <!-- ── Chat panel ── -->
  <div class="chat-panel">
    <div class="messages" id="messages"></div>
    <div class="input-bar">
      <div class="quick-btns" id="quick-btns">
        <button class="qbtn" onclick="sendQuick('check all services')">&#9654; Check All</button>
        <button class="qbtn" onclick="sendQuick('is EAI up?')">EAI status</button>
        <button class="qbtn" onclick="sendQuick('is cans up?')">cans status</button>
        <button class="qbtn" onclick="sendQuick('is rmq-producer up?')">rmq status</button>
        <button class="qbtn" onclick="sendQuick('is wmq-file-integrator up?')">wmq status</button>
        <button class="qbtn" onclick="sendQuick('help')">&#63; Help</button>
      </div>
      <div class="input-row">
        <input id="msg-input" type="text"
               placeholder="e.g.  is EAI up?  /  restart cans  /  check all on sit"
               autocomplete="off" />
        <button id="send-btn" onclick="sendMsg()">Send</button>
        <button id="abort-btn" onclick="abortRequest()" style="display:none; background:#f85149; color:#fff; border:none; border-radius:4px; padding:7px 14px; cursor:pointer; font-family:inherit; font-size:13px; font-weight:bold;">&#215; Abort</button>
      </div>
    </div>
  </div>

  <!-- ── Status dashboard ── -->
  <div class="dash-panel">
    <div class="dash-header">
      <span class="dash-title">&#128200; Live Status</span>
      <button id="refresh-btn" onclick="refreshDash()">&#8635; Refresh</button>
    </div>
    <div class="dash-body">
      <div class="dash-env-label" id="dash-env-label"></div>
      <table class="dash-table">
        <thead><tr><th>Service</th><th>Status</th><th>PID</th><th>Act.</th></tr></thead>
        <tbody id="dash-tbody"><tr><td colspan="4" style="color:#8b949e">Loading...</td></tr></tbody>
      </table>
      <div class="last-updated" id="last-updated"></div>
    </div>
  </div>

</div>

<script>
var currentEnv = '030';
var SERVERS    = {};
var busy       = false;
var currentAbortController = null;

// ── Init ──────────────────────────────────────────────────────────────────────
fetch('/api/envs')
  .then(function(r){ return r.json(); })
  .then(function(data){
    SERVERS = {};
    data.envs.forEach(function(e){ SERVERS[e.key] = e; });

    var bar = document.getElementById('env-bar');
    data.envs.forEach(function(e){
      var btn = document.createElement('button');
      btn.className = 'env-btn' + (e.key === currentEnv ? ' active' : '');
      btn.textContent = e.label;
      btn.setAttribute('data-key', e.key);
      btn.onclick = function(){ switchEnv(e.key); };
      bar.appendChild(btn);
    });

    updateConnInfo();
    refreshDash();
    botMsg('Connected to <b>[' + envLabel(currentEnv) + ']</b> &#8594; '
           + (SERVERS[currentEnv] ? SERVERS[currentEnv].host : currentEnv)
           + '<br>Type a command or click a quick button below.');
  });

// ── Env helpers ───────────────────────────────────────────────────────────────
function envLabel(key){
  return SERVERS[key] ? SERVERS[key].label : key.toUpperCase();
}

function switchEnv(key){
  currentEnv = key;
  document.querySelectorAll('.env-btn').forEach(function(b){
    b.classList.toggle('active', b.getAttribute('data-key') === key);
  });
  updateConnInfo();
  refreshDash();
  botMsg('Switched to <b>[' + envLabel(key) + ']</b> &#8594; '
         + (SERVERS[key] ? SERVERS[key].host : key));
}

function updateConnInfo(){
  var el = document.getElementById('conn-info');
  var s  = SERVERS[currentEnv];
  el.textContent = s ? s.host + ' (' + s.label + ')' : currentEnv;
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
function refreshDash(){
  var btn  = document.getElementById('refresh-btn');
  var tbody = document.getElementById('dash-tbody');
  btn.innerHTML = '<span class="spinning">&#8635;</span>';
  btn.disabled  = true;

  fetch('/api/status?env=' + currentEnv)
    .then(function(r){ return r.json(); })
    .then(function(data){
      var html = '';
      var up   = 0;
      data.services.forEach(function(s){
        var state = s.state || 'UNKNOWN';
        var isUp  = state.indexOf('UP') === 0;
        if(isUp) up++;
        var dotClass = isUp ? 'dot-up' : (state.indexOf('STALE') > -1 ? 'dot-stale' : 'dot-down');
        var dot = '<span class="' + dotClass + '">&#9679;</span>';
        html += '<tr>'
          + '<td><b>' + s.service + '</b></td>'
          + '<td>' + dot + ' ' + state + '</td>'
          + '<td>' + (s.pid || '-') + '</td>'
          + '<td class="action-cell">'
          + '<button title="Start"  onclick="quickAction(&quot;start&quot;,&quot;' + s.service + '&quot;,&quot;' + currentEnv + '&quot;)">&#9654;</button>'
          + '<button title="Stop"   onclick="quickAction(&quot;stop&quot;,&quot;'  + s.service + '&quot;,&quot;' + currentEnv + '&quot;)">&#9632;</button>'
          + '<button title="Restart" onclick="quickAction(&quot;restart&quot;,&quot;' + s.service + '&quot;,&quot;' + currentEnv + '&quot;)">&#8635;</button>'
          + '</td></tr>';
      });
      tbody.innerHTML = html || '<tr><td colspan="4">No services found.</td></tr>';
      var sc = up === data.services.length ? '#3fb950' : (up > 0 ? '#d29922' : '#f85149');
      document.getElementById('dash-env-label').innerHTML =
        '<span style="color:' + sc + '">' + up + '/' + data.services.length + ' running</span>'
        + ' on [' + data.label + ']';
      document.getElementById('last-updated').textContent =
        'Updated: ' + new Date().toLocaleTimeString();
      btn.innerHTML = '&#8635; Refresh';
      btn.disabled  = busy;
    })
    .catch(function(e){
      tbody.innerHTML = '<tr><td colspan="4" style="color:#f85149">Error: ' + e + '</td></tr>';
      btn.innerHTML = '&#8635; Refresh';
      btn.disabled  = busy;
    });
}

// Auto-refresh every 15s for tight live status synchronization
setInterval(refreshDash, 15000);

// ── Quick actions from dashboard buttons ──────────────────────────────────────
function quickAction(intent, service, envKey){
  if(busy) return;
  var payload = '__action__:' + intent + ':' + service + ':' + envKey;
  userMsg(intent.charAt(0).toUpperCase() + intent.slice(1) + ' ' + service + ' [' + envLabel(envKey) + ']');
  postChat(payload, envKey);
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function sendMsg(){
  if(busy) return;
  var input = document.getElementById('msg-input');
  var text  = input.value.trim();
  if(!text) return;
  input.value = '';
  userMsg(text);
  postChat(text, currentEnv);
}

function sendQuick(text){
  if(busy) return;
  userMsg(text);
  postChat(text, currentEnv);
}

function abortRequest(){
  if (currentAbortController) {
    currentAbortController.abort();
    currentAbortController = null;
  }
  chatQueue = [];
  processingQueue = false;
  botMsg('&#9888; <span style="color:#f85149">Command execution cancelled by user.</span>');
  setBusy(false);
  refreshDash();
}

var chatQueue = [];
var processingQueue = false;

function postChat(text, env){
  chatQueue.push({text: text, env: env});
  processChatQueue();
}

function processChatQueue(){
  if (processingQueue || chatQueue.length === 0) {
    if (chatQueue.length === 0) {
      setBusy(false);
    }
    return;
  }
  processingQueue = true;
  setBusy(true);
  
  var next = chatQueue.shift();
  var thinking = botMsg('<span class="spinning">&#8635;</span> thinking...', true);
  
  currentAbortController = new AbortController();
  fetch('/api/chat', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({message: next.text, env: next.env}),
    signal:  currentAbortController.signal
  })
  .then(function(r){ return r.json(); })
  .then(function(data){
    currentAbortController = null;
    thinking.remove();
    renderResponse(data);
    
    // Instantly refresh live dashboard table as soon as command finishes
    refreshDash();
    
    processingQueue = false;
    processChatQueue();
  })
  .catch(function(e){
    currentAbortController = null;
    thinking.remove();
    if(e.name === 'AbortError') return;
    botMsg('&#10060; Error: ' + e);
    processingQueue = false;
    processChatQueue();
  });
}

function renderResponse(data){
  if(data.type === 'choice'){
    var row = botMsg(data.html || '');
    if(data.options && data.options.length){
      var optRow = document.createElement('div');
      optRow.className = 'options-row';
      data.options.forEach(function(opt){
        var btn = document.createElement('button');
        btn.className = 'opt-btn';
        btn.textContent = opt.label;
        btn.onclick = function(){
          if(busy) return;
          optRow.remove();
          userMsg(opt.label);
          postChat(opt.value, currentEnv);
        };
        optRow.appendChild(btn);
      });
      row.appendChild(optRow);
    }
  } else {
    botMsg(data.html || '(empty response)');
  }
}

// ── Message rendering ─────────────────────────────────────────────────────────
function userMsg(text){
  var row = document.createElement('div');
  row.className = 'bubble-row user';
  var bub = document.createElement('div');
  bub.className = 'bubble user';
  bub.textContent = text;
  row.appendChild(bub);
  msgs().appendChild(row);
  scrollBottom();
  return row;
}

function botMsg(html, thinking){
  var row = document.createElement('div');
  row.className = 'bubble-row bot';
  var bub = document.createElement('div');
  bub.className = 'bubble bot' + (thinking ? ' thinking' : '');
  bub.innerHTML = html;
  row.appendChild(bub);
  msgs().appendChild(row);
  scrollBottom();
  return row;
}

function msgs(){ return document.getElementById('messages'); }
function scrollBottom(){ var m = msgs(); m.scrollTop = m.scrollHeight; }

function setBusy(b){
  busy = b;
  var input = document.getElementById('msg-input');
  var sendBtn = document.getElementById('send-btn');
  var abortBtn = document.getElementById('abort-btn');
  
  if (input) {
    input.disabled = b;
    input.placeholder = b ? 'Executing command... Click Abort to cancel' : 'e.g.  is EAI up?  /  restart cans  /  check all on sit';
  }
  if (sendBtn) {
    sendBtn.disabled = b;
  }
  if (abortBtn) {
    abortBtn.style.display = b ? 'inline-block' : 'none';
  }
  
  // Visually grey out and disable all interactive elements
  var selectors = ['.env-btn', '.qbtn', '.action-cell button', '#refresh-btn', '.opt-btn'];
  selectors.forEach(function(sel){
    document.querySelectorAll(sel).forEach(function(el){
      el.disabled = b;
      el.style.opacity = b ? '0.3' : '1';
      el.style.pointerEvents = b ? 'none' : 'auto';
      el.style.cursor = b ? 'not-allowed' : 'pointer';
    });
  });
}

// ── Enter key ─────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function(){
  document.getElementById('msg-input').addEventListener('keydown', function(e){
    if(e.key === 'Enter') sendMsg();
  });
});
</script>
</body>
</html>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress noisy access log

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]

        if path in ('/', '/index.html'):
            self._send_html(HTML_PAGE)
            return

        if path == '/api/envs':
            self._send_json({'envs': env_list()})
            return

        if path == '/api/status':
            qs = self.path[self.path.find('?')+1:] if '?' in self.path else ''
            env_key = '030'
            for part in qs.split('&'):
                if part.startswith('env='):
                    env_key = part[4:]
            if env_key not in SERVERS:
                self._send_json({'error': 'unknown env'}, 400)
                return
            results = svc_status_all(env_key)
            self._send_json({'env': env_key, 'label': env_label(env_key), 'services': results})
            return

        if path == '/api/apis-status':
            qs = self.path[self.path.find('?')+1:] if '?' in self.path else ''
            env_key = '030'
            for part in qs.split('&'):
                if part.startswith('env='):
                    env_key = part[4:]
            if env_key not in SERVERS:
                self._send_json({'error': 'unknown env'}, 400)
                return
            if not VAULT_READY or VAULT is None:
                self._send_json({'error': 'Vault not configured. Fill in role_id and secret_id in config.ini', 'apis': []})
                return
            sc      = SERVERS[env_key]
            results = check_all_apis(VAULT, sc)
            self._send_json({'env': env_key, 'label': env_label(env_key), 'apis': results})
            return

        if path == '/api/apis':
            # Returns the list of configured APIs (names + aliases, no secrets)
            api_list = [{'name': a.get('name'), 'aliases': a.get('aliases', []),
                         'team': a.get('team', ''), 'contact': a.get('contact', '')}
                        for a in get_apis()]
            self._send_json({'apis': api_list})
            return

        self.send_error(404)

    def do_POST(self):
        if self.path == '/api/chat':
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length).decode('utf-8'))
                msg = body.get('message', '').strip()
                env = body.get('env', '030')
                if env not in SERVERS:
                    env = list(SERVERS.keys())[0]
                resp = process_message(msg, env)
                self._send_json(resp)
            except Exception as exc:
                self._send_json({'type': 'text', 'html': 'Server error: {0}'.format(str(exc))})
            return
        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    host = '0.0.0.0'

    server = ThreadedHTTPServer((host, port), Handler)

    print('')
    print('  UAT Ops Chatbot  (Web UI)')
    print('  ' + '-' * 45)
    print('  Listening : http://{0}:{1}'.format(host, port))
    print('')
    print('  From your laptop (SSH tunnel):')
    print('    ssh -L {p}:localhost:{p} cpndev01@cpnuatap030'.format(p=port))
    print('    Open: http://localhost:{0}'.format(port))
    print('')
    print('  Or if server IP is reachable on your network:')
    print('    http://cpnuatap030:{0}'.format(port))
    print('')
    print('  Ctrl+C to stop.')
    print('')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Stopped.')


if __name__ == '__main__':
    main()
