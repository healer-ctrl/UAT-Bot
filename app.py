#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — UAT Ops Chatbot main loop.

stdlib only. Python 3.6+ compatible.
No paramiko, no pip dependencies.
Remote execution via system 'ssh' binary (key-based auth required).
Local execution via Popen + tempfile (avoids PIPE deadlock with daemonizing scripts).

Run: python3 app.py
"""

import os
import re
import sys
import socket
import tempfile
import subprocess
import configparser

from intents import (
    classify_intent,
    is_all_request,
    extract_service,
    suggest_services,
    extract_env,
)

# ── Config ────────────────────────────────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(_SCRIPT_DIR, 'config.ini')


def load_config():
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE, encoding='utf-8')

    general = dict(cfg['general']) if 'general' in cfg else {}

    service_aliases = {}
    if 'service_aliases' in cfg:
        for k, v in cfg['service_aliases'].items():
            service_aliases[k.strip()] = v.strip()

    servers = {}
    for section in cfg.sections():
        if section.startswith('server:'):
            key = section.split(':', 1)[1].strip()
            servers[key] = dict(cfg[section])
            raw = servers[key].get('services', '')
            servers[key]['services'] = [s.strip() for s in raw.split(',') if s.strip()]

    return general, service_aliases, servers


def get_all_services(servers):
    """Deduplicated list of every service across all servers."""
    seen = []
    for sc in servers.values():
        for svc in sc.get('services', []):
            if svc not in seen:
                seen.append(svc)
    return seen


# ── SSH / local execution ─────────────────────────────────────────────────────

def _my_hostname():
    try:
        return socket.gethostname().lower()
    except Exception:
        return ''


def is_local(host):
    """True when 'host' resolves to the machine we're running on."""
    h = host.lower()
    me = _my_hostname()
    return h in me or me in h or h == 'localhost' or h == '127.0.0.1'


def _run_local(cmd):
    """
    Run a shell command locally.
    Uses Popen + temp files — NOT PIPE — because Python 3.6's
    communicate() can deadlock when a script forks a background
    daemon that keeps the pipe fd open.
    Returns (stdout_str, stderr_str, returncode).
    """
    tmp_out = tempfile.NamedTemporaryFile(mode='w', suffix='.out', delete=False)
    tmp_err = tempfile.NamedTemporaryFile(mode='w', suffix='.err', delete=False)
    out_path = tmp_out.name
    err_path = tmp_err.name
    tmp_out.close()
    tmp_err.close()

    try:
        with open(out_path, 'w') as fout, open(err_path, 'w') as ferr:
            proc = subprocess.Popen(
                cmd, shell=True,
                stdout=fout, stderr=ferr,
                preexec_fn=os.setsid,   # new process group so daemon children don't block us
            )
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                return '', 'Timed out after 30s', 1

        with open(out_path, 'r') as f:
            stdout = f.read()
        with open(err_path, 'r') as f:
            stderr = f.read()

        return stdout, stderr, proc.returncode

    except Exception as exc:
        return '', str(exc), 1

    finally:
        for p in (out_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _run_remote(host, user, cmd, password=None):
    """
    Run a shell command on a remote host via the system ssh binary.
    """
    if password:
        ssh_cmd = 'sshpass -p "{password}" ssh -o BatchMode=no -o ConnectTimeout=10 -o StrictHostKeyChecking=no {user}@{host} "{cmd}"'.format(
            password=password.replace('"', '\\"'), user=user, host=host, cmd=cmd.replace('"', '\\"')
        )
    else:
        ssh_cmd = 'ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no {user}@{host} "{cmd}"'.format(
            user=user, host=host, cmd=cmd.replace('"', '\\"')
        )
    try:
        result = subprocess.run(
            ssh_cmd, shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        stdout = result.stdout.decode('utf-8', errors='replace')
        stderr = result.stderr.decode('utf-8', errors='replace')
        return stdout, stderr, result.returncode
    except subprocess.TimeoutExpired:
        return '', 'SSH connection timed out (30s)', 1
    except Exception as exc:
        return '', str(exc), 1


def run_script(server_cfg, script_name, service, general):
    """
    Build the shell command and dispatch to local or remote runner.
    Returns (stdout, stderr, returncode).
    """
    host        = server_cfg.get('host.' + service, server_cfg['host'])
    user        = server_cfg.get('user.' + service, server_cfg.get('user', general.get('default_user', 'cpndev01')))
    scripts_dir = server_cfg.get('scripts_dir.' + service, server_cfg['scripts_dir'])
    password    = server_cfg.get('password.' + service, server_cfg.get('password', None))
    
    if script_name == general.get('status_script', 'app_status.sh') and service in ('si', 'batch'):
        cmd = 'jps'
    else:
        actual_script = script_name
        if script_name == general.get('start_script', 'app_start.sh'):
            actual_script = server_cfg.get('start_script.' + service, 'startSI' if service == 'si' else 'startNAPAll.sh')
        elif script_name == general.get('stop_script', 'app_stop.sh'):
            actual_script = server_cfg.get('stop_script.' + service, 'stopSI' if service == 'si' else 'stopNAPAll.sh')
        cmd = 'sh {dir}/{script} {svc}'.format(
            dir=scripts_dir, script=actual_script, svc=service
        )

    if is_local(host):
        return _run_local(cmd)
    else:
        return _run_remote(host, user, cmd, password)


# ── Status output parsing ─────────────────────────────────────────────────────

def parse_status(output):
    """
    Parse app_status.sh output.
    Contract:
      'is running'          → UP
      'PID file not found'  → DOWN (no pid file)
      'Process not running' → DOWN (stale pid)
    """
    if 'is running' in output:
        return 'UP'
    if 'PID file not found' in output:
        return 'DOWN'
    if 'Process not running' in output:
        m = re.search(r'Process not running:\s*(\d+)', output)
        pid = m.group(1) if m else '?'
        return 'DOWN (stale pid {0})'.format(pid)
    if output.strip() == '':
        return 'NO OUTPUT'
    return 'UNKNOWN'


def extract_pid(output):
    m = re.search(r'(\d{3,6}) is running', output)
    return m.group(1) if m else '-'


# ── Action helpers ────────────────────────────────────────────────────────────

def _env_label(server_key, servers):
    sc = servers.get(server_key, {})
    return sc.get('aliases', server_key).split(',')[0].strip().upper()


def check_svc_status(server_cfg, service, general):
    stdout, stderr, _ = run_script(server_cfg, general.get('status_script', 'app_status.sh'), service, general)
    if stderr.strip() and not stdout.strip():
        return 'ERROR', '-', stderr.strip()[:200]
        
    if service == 'si':
        count = stdout.count('MasterThread')
        state = 'UP' if count >= 2 else 'DOWN'
        return state, '-', '{0} ({1}/2 MasterThread)'.format(state, count)
    elif service == 'batch':
        count = stdout.count('NCSAsynchronousProcessor')
        state = 'UP' if count >= 5 else 'DOWN'
        return state, '-', '{0} ({1}/5 NAP)'.format(state, count)
        
    state = parse_status(stdout)
    pid = extract_pid(stdout) if state == 'UP' else '-'
    return state, pid, state


def do_status(server_key, service, servers, general):
    sc    = servers[server_key]
    label = _env_label(server_key, servers)
    print('  Checking {svc} on [{env}]...'.format(svc=service, env=label))

    state, pid, display_state = check_svc_status(sc, service, general)
    if state == 'ERROR':
        print('  ERROR: {0}'.format(display_state))
        return

    icon  = 'UP  ✅' if state == 'UP' else 'DOWN ❌'
    print('  [{env}] {svc}: {state} | PID: {pid}'.format(
        env=label, svc=service, state=icon if service not in ('si', 'batch') else display_state, pid=pid))


def do_status_all(server_key, servers, general):
    sc       = servers[server_key]
    label    = _env_label(server_key, servers)
    services = sc.get('services', [])

    W = 28   # column width
    print('')
    print('  Server : {host}  [{env}]'.format(host=sc['host'], env=label))
    print('  User   : {user}'.format(user=sc.get('user', 'cpndev01')))
    print('  ' + '-' * 52)
    print('  {:<{w}} {:<14} {}'.format('Service', 'Status', 'PID', w=W))
    print('  ' + '-' * 52)

    up_count = 0
    for svc in services:
        state, pid, display_state = check_svc_status(sc, svc, general)
        icon  = '✅' if state == 'UP' else '❌'
        if state == 'UP':
            up_count += 1
        print('  {icon} {svc:<{w}} {state:<14} {pid}'.format(
            icon=icon, svc=svc, w=W - 2, state=display_state, pid=pid))

    print('  ' + '-' * 52)
    print('  {up}/{total} services running on [{env}]\n'.format(
        up=up_count, total=len(services), env=label))


def do_start(server_key, service, servers, general):
    sc    = servers[server_key]
    label = _env_label(server_key, servers)
    print('  Starting {svc} on [{env}]...'.format(svc=service, env=label))

    stdout, stderr, rc = run_script(sc, general.get('start_script', 'app_start.sh'), service, general)

    if rc == 0:
        print('  ✅ Start command sent for {0}'.format(service))
    else:
        print('  ❌ Start failed for {svc}: {err}'.format(svc=service, err=(stderr or stdout).strip()[:200]))

    if stdout.strip():
        print('  >> {0}'.format(stdout.strip()[:200]))


def do_stop(server_key, service, servers, general):
    sc    = servers[server_key]
    label = _env_label(server_key, servers)
    print('  Stopping {svc} on [{env}]...'.format(svc=service, env=label))

    stdout, stderr, rc = run_script(sc, general.get('stop_script', 'app_stop.sh'), service, general)

    if rc == 0:
        print('  ✅ Stop command sent for {0}'.format(service))
    else:
        print('  ❌ Stop failed for {svc}: {err}'.format(svc=service, err=(stderr or stdout).strip()[:200]))

    if stdout.strip():
        print('  >> {0}'.format(stdout.strip()[:200]))


def do_restart(server_key, service, servers, general):
    do_stop(server_key, service, servers, general)
    do_start(server_key, service, servers, general)


# ── Execute intent ────────────────────────────────────────────────────────────

def execute_intent(intent, service, server_key, servers, general):
    if intent == 'start':
        do_start(server_key, service, servers, general)
    elif intent == 'stop':
        do_stop(server_key, service, servers, general)
    elif intent == 'restart':
        do_restart(server_key, service, servers, general)
    else:
        do_status(server_key, service, servers, general)


# ── Help / meta ───────────────────────────────────────────────────────────────

HELP_TEXT = """
+-------------------------------------------------------------+
|         UAT Ops Chatbot  —  Command Reference               |
+-------------------------------------------------------------+
 STATUS:
   is EAI up?                 check one service
   check all services         check all services on current env
   check all on sit           check all on SIT/027
   is everything fine?        same as "check all"
   health check               same as "check all"

 START / STOP / RESTART:
   start EAI                  start a service
   stop cans                  stop a service
   restart rmq-producer       stop then immediately start
   kill wmq                   same as stop
   bring up EAI               same as start

 ENVIRONMENT:
   switch to sit              change current env to 027
   use prepro                 change to 036 (JPPrepro)
   connect to 030             change to 030 (UAT)
   which server am i on?      show current env + host

 OTHER:
   list services              show known services on current env
   help                       show this help
   exit / quit                exit chatbot

 ENVIRONMENTS  →  alias examples:
   UAT     030  uat  uatap030
   PrePro  036  jpp  jprepro  prepro
   SIT     027  sit  dev  jsit
+-------------------------------------------------------------+
"""


def print_help():
    print(HELP_TEXT)


def print_services(server_key, servers):
    sc    = servers.get(server_key, {})
    label = _env_label(server_key, servers)
    svcs  = sc.get('services', [])
    print('  Services on [{env}]:'.format(env=label))
    for svc in svcs:
        print('    - {0}'.format(svc))


# ── Disambiguation helpers ────────────────────────────────────────────────────

def _ask_which_service(options, intent):
    """Print disambiguation prompt and return pending dict."""
    print('  Multiple matches — did you mean:')
    for i, s in enumerate(options):
        print('    {0}. {1}'.format(i + 1, s))
    return {'type': 'service', 'options': options, 'intent': intent}


def _ask_which_env(service, candidate_keys, intent, servers):
    """Print env disambiguation prompt and return pending dict."""
    print('  {svc} exists on multiple environments — which one?'.format(svc=service))
    for i, k in enumerate(candidate_keys):
        sc = servers[k]
        print('    {0}. [{1}] {2}'.format(i + 1, _env_label(k, servers), sc['host']))
    return {'type': 'env', 'options': candidate_keys, 'intent': intent, 'service': service}


def _resolve_number_or_text(user_input, options):
    """
    If user typed a number return options[n-1], else try to match text.
    Returns chosen item or None.
    """
    text = user_input.strip()
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(options):
            return options[idx]
        return None
    # text match
    if text in options:
        return text
    # case-insensitive
    for o in options:
        if o.lower() == text.lower():
            return o
    return None


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    general, service_aliases, servers = load_config()

    if not servers:
        print('ERROR: No servers configured in config.ini — check the [server:XXX] sections.')
        sys.exit(1)

    # Default environment: 030/UAT if present, else first defined
    current_key = '030' if '030' in servers else list(servers.keys())[0]
    all_services = get_all_services(servers)

    # pending: tracks multi-turn disambiguation state
    pending = {}   # keys: type, options, intent, [service], [server_key]

    print('')
    print('  UAT Ops Chatbot  |  type "help" for commands, "exit" to quit')
    print('  Connected to: [{env}] → {host}'.format(
        env=_env_label(current_key, servers),
        host=servers[current_key]['host']))
    print('')

    while True:
        try:
            user_input = input('You: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\n  Bye!')
            break

        if not user_input:
            continue

        lower = user_input.lower()

        # ── Exit ─────────────────────────────────────────────────────────────
        if lower in ('exit', 'quit', 'bye', 'q', ':q'):
            print('  Bye!')
            break

        # ── Help ─────────────────────────────────────────────────────────────
        if lower in ('help', '?', 'h', 'commands'):
            print_help()
            continue

        # ── List services ─────────────────────────────────────────────────────
        if re.search(r'\blist\b.*(service|app)', lower) or lower in ('services', 'apps'):
            print_services(current_key, servers)
            continue

        # ── Which server ──────────────────────────────────────────────────────
        if re.search(r'\b(which|current|where|what).*(server|env|environment|host)\b', lower) \
                or 'am i on' in lower or 'which server' in lower:
            sc = servers[current_key]
            print('  Current: [{env}] → {host}  (user: {user})'.format(
                env=_env_label(current_key, servers),
                host=sc['host'],
                user=sc.get('user', 'cpndev01')))
            continue

        # ── Pending disambiguation (multi-turn) ───────────────────────────────
        if pending:
            ptype = pending.get('type')

            if ptype == 'service':
                chosen = _resolve_number_or_text(user_input, pending['options'])
                if chosen:
                    intent     = pending['intent']
                    server_key = pending.get('server_key', current_key)
                    pending    = {}
                    execute_intent(intent, chosen, server_key, servers, general)
                else:
                    print('  Not recognised. Please type a number or one of: {0}'.format(
                        ', '.join(pending['options'])))
                continue

            if ptype == 'env':
                chosen = _resolve_number_or_text(user_input, pending['options'])
                if not chosen:
                    # maybe user typed an alias
                    extracted = extract_env(user_input, servers)
                    if extracted and extracted in pending['options']:
                        chosen = extracted
                if chosen:
                    intent  = pending['intent']
                    service = pending['service']
                    pending = {}
                    execute_intent(intent, service, chosen, servers, general)
                else:
                    print('  Not recognised. Options: {0}'.format(
                        ', '.join('[{0}]'.format(_env_label(k, servers)) for k in pending['options'])))
                continue

        # ── Environment switch ────────────────────────────────────────────────
        _SWITCH_TRIGGERS = ('switch to', 'use ', 'connect to', 'change to',
                            'go to ', 'change env', 'switch env')
        is_switch = any(t in lower for t in _SWITCH_TRIGGERS)
        if is_switch:
            extracted = extract_env(user_input, servers)
            if extracted:
                current_key = extracted
                sc = servers[current_key]
                print('  Switched to [{env}] → {host}'.format(
                    env=_env_label(current_key, servers), host=sc['host']))
            else:
                print('  Unknown environment. Available:')
                for k, sc in servers.items():
                    print('    [{env}]  host={host}  aliases: {aliases}'.format(
                        env=_env_label(k, servers),
                        host=sc['host'],
                        aliases=sc.get('aliases', '')))
            continue

        # ── Parse intent and env ──────────────────────────────────────────────
        intent   = classify_intent(user_input)
        is_all   = is_all_request(user_input)
        target   = extract_env(user_input, servers) or current_key
        sc       = servers[target]

        # ── "All services" branch ─────────────────────────────────────────────
        if is_all:
            if intent == 'status':
                do_status_all(target, servers, general)

            elif intent == 'start':
                for svc in sc.get('services', []):
                    do_start(target, svc, servers, general)

            elif intent in ('stop', 'restart'):
                verb = 'stop' if intent == 'stop' else 'restart'
                print('  About to {v} ALL services on [{env}]. Are you sure? (yes/no)'.format(
                    v=verb.upper(), env=_env_label(target, servers)))
                confirm = input('You: ').strip().lower()
                if confirm in ('yes', 'y'):
                    for svc in sc.get('services', []):
                        if intent == 'stop':
                            do_stop(target, svc, servers, general)
                        else:
                            do_restart(target, svc, servers, general)
                else:
                    print('  Cancelled.')
            continue

        # ── Single service branch ─────────────────────────────────────────────
        service = extract_service(user_input, sc.get('services', []), service_aliases)

        # -- Service NOT found directly --
        if not service:
            # Try against all services across all envs
            all_svc_list = get_all_services(servers)
            suggestions  = suggest_services(user_input, all_svc_list, service_aliases)

            if not suggestions:
                print("  Hmm, I didn't catch a service name. Try 'check all services' or 'is EAI up'.")
                print('  Known services: {0}'.format(', '.join(all_svc_list)))
                continue

            if len(suggestions) == 1:
                svc = suggestions[0]
                print("  Did you mean '{0}'? (yes/no)".format(svc))
                confirm = input('You: ').strip().lower()
                if confirm not in ('yes', 'y', ''):
                    print("  OK — let me know what you need. Type 'help' for commands.")
                    continue
                service = svc
                # Fall through to resolve server below

            else:
                pending = _ask_which_service(suggestions, intent)
                pending['server_key'] = target
                continue

        # -- Service found: make sure it's on the target server --
        if service not in sc.get('services', []):
            candidate_keys = [k for k, s in servers.items()
                              if service in s.get('services', [])]
            if len(candidate_keys) == 0:
                print("  '{0}' not found on any configured server.".format(service))
                continue
            elif len(candidate_keys) == 1:
                target = candidate_keys[0]
            else:
                # service on multiple servers and user didn't specify env
                pending = _ask_which_env(service, candidate_keys, intent, servers)
                continue

        execute_intent(intent, service, target, servers, general)


if __name__ == '__main__':
    main()
