#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
api_checker.py  --  Poll external APIs for health using OAuth2 + Vault secrets.
stdlib only. Python 3.6+. No pip.

Health check logic:
  2xx        → UP   ✅  (server responded normally)
  4xx        → UP   ✅  (API alive; probe request had no valid params — expected)
  5xx        → DOWN ❌  (server-side error)
  timeout    → UNREACHABLE ❌
  conn error → UNREACHABLE ❌
  auth error → AUTH ERROR ⚠️
"""

import re
import ssl
import time
import json
import os
import socket
import threading
import urllib.request
import urllib.error

from vault_client import get_oauth2_token, _ssl_ctx


# ── Load apis.json ────────────────────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))
_APIS_PATH = os.path.join(_DIR, 'apis.json')

_APIS_CONFIG = None


def load_apis():
    global _APIS_CONFIG
    try:
        with open(_APIS_PATH, 'r') as f:
            _APIS_CONFIG = json.load(f)
    except Exception as ex:
        _APIS_CONFIG = {'apis': [], '_load_error': str(ex)}
    return _APIS_CONFIG


def get_apis():
    if _APIS_CONFIG is None:
        load_apis()
    return _APIS_CONFIG.get('apis', [])


def get_flow_check_api_names():
    cfg = _APIS_CONFIG or load_apis()
    return cfg.get('_flow_check_apis', [])


# ── Env DNS helper ────────────────────────────────────────────────────────────

def get_env_dns_label(server_cfg):
    """Resolve standard juat/jpreprod/jsit name from server aliases."""
    if not server_cfg:
        return 'juat'
    aliases = [a.strip().lower() for a in server_cfg.get('aliases', '').split(',')]
    if 'juat' in aliases or 'uat' in aliases or '030' in aliases:
        return 'juat'
    if 'jpreprod' in aliases or 'jpp' in aliases or 'preprod' in aliases or '036' in aliases:
        return 'jpreprod'
    if 'jsit' in aliases or 'sit' in aliases or '027' in aliases:
        return 'jsit'
    return aliases[0] if aliases else 'juat'


# ── URL cleanup ───────────────────────────────────────────────────────────────

def _clean_url(url):
    """
    Strip path parameters and query strings from a URL for use as a health probe.
    e.g. 'https://host/api/{id}/events/{eventId}?foo=1' → 'https://host/api'
    """
    # Strip query string
    url = url.split('?')[0]
    # Strip from first {param} onwards
    m = re.search(r'(.+?)(?:/\{[^}]+\})', url)
    if m:
        url = m.group(1)
    return url.rstrip('/')


# ── Single API health check ───────────────────────────────────────────────────

def check_api(api_cfg, vault_secrets, ssl_ctx_obj=None, server_cfg=None):
    """
    Health-check a single API (HTTP, Actuator, or TCP).

    api_cfg:       one entry from apis.json
    vault_secrets: dict fetched from Vault (all key-value pairs)
    ssl_ctx_obj:   ssl.SSLContext or None
    server_cfg:    optional server config dict

    Returns dict:
      {
        'name':            str,
        'state':           'UP' | 'DOWN' | 'UNREACHABLE' | 'AUTH_ERROR' | 'CONFIG_ERROR' | 'SKIP',
        'status_code':     int or None,
        'response_time_ms': int,
        'team':            str,
        'contact':         str,
        'error':           str or None,
      }
    """
    name    = api_cfg.get('name', 'Unknown API')
    team    = api_cfg.get('team', '')
    contact = api_cfg.get('contact', '')
    timeout = api_cfg.get('timeout_sec', 10)

    result = {
        'name':             name,
        'state':            'UNKNOWN',
        'status_code':      None,
        'response_time_ms': 0,
        'team':             team,
        'contact':          contact,
        'error':            None,
    }

    # ── TCP socket check option ───────────────────────────────────────────────
    if api_cfg.get('type') == 'tcp':
        host = api_cfg.get('host', '')
        port = api_cfg.get('port', 80)
        t0 = time.time()
        try:
            # Simple TCP connect check
            with socket.create_connection((host, port), timeout=timeout) as sock:
                elapsed = int((time.time() - t0) * 1000)
                result['state'] = 'UP'
                result['response_time_ms'] = elapsed
        except Exception as exc:
            elapsed = int((time.time() - t0) * 1000)
            result['state'] = 'DOWN'
            result['response_time_ms'] = elapsed
            result['error'] = str(exc)[:200]
        return result

    # ── Resolve health URL (Actuator or Vault-key) ───────────────────────────
    if api_cfg.get('type') == 'actuator':
        env_dns = get_env_dns_label(server_cfg)
        template = api_cfg.get('health_url_template', '')
        health_url = template.replace('{env_label}', env_dns)
    else:
        url_key = api_cfg.get('health_url_key', '')
        raw_url = vault_secrets.get(url_key, '') if url_key else api_cfg.get('health_url', '')

        if not raw_url:
            result['state'] = 'SKIP'
            result['error'] = 'No health URL configured (key: {0})'.format(url_key)
            return result

        health_url = _clean_url(raw_url)

    # ── Build auth headers ────────────────────────────────────────────────────
    headers   = {}
    auth_cfg  = api_cfg.get('auth', {})
    auth_type = auth_cfg.get('type', 'none')

    if auth_type == 'oauth2_vault':
        client_id     = vault_secrets.get(auth_cfg.get('client_id_key', ''), '')
        client_secret = vault_secrets.get(auth_cfg.get('client_secret_key', ''), '')
        token_url     = vault_secrets.get(auth_cfg.get('token_url_key', ''),
                                          auth_cfg.get('token_url', ''))
        scopes        = vault_secrets.get(auth_cfg.get('scopes_key', ''),
                                          auth_cfg.get('scopes', ''))

        if not client_id or not client_secret or not token_url:
            result['state'] = 'CONFIG_ERROR'
            result['error'] = 'Missing OAuth2 credentials in Vault secret for: {0}'.format(name)
            return result

        try:
            token = get_oauth2_token(token_url, client_id, client_secret, scopes, ssl_ctx_obj)
            headers['Authorization'] = 'Bearer {0}'.format(token)
        except Exception as exc:
            result['state'] = 'AUTH_ERROR'
            result['error'] = str(exc)[:250]
            return result

    # ── Poll the API ──────────────────────────────────────────────────────────
    t0 = time.time()

    try:
        req = urllib.request.Request(health_url, headers=headers)
        try:
            with urllib.request.urlopen(req, context=ssl_ctx_obj, timeout=timeout) as resp:
                status_code = resp.getcode()
        except urllib.error.HTTPError as exc:
            status_code = exc.code

        elapsed = int((time.time() - t0) * 1000)
        result['status_code']      = status_code
        result['response_time_ms'] = elapsed

        if 200 <= status_code < 300:
            result['state'] = 'UP'
        elif 400 <= status_code < 500:
            # 4xx means API is reachable; our probe just has no valid params — treat as UP
            result['state'] = 'UP'
        else:
            result['state'] = 'DOWN'
            result['error'] = 'HTTP {0}'.format(status_code)

    except urllib.error.URLError as exc:
        elapsed = int((time.time() - t0) * 1000)
        result['state']             = 'UNREACHABLE'
        result['response_time_ms']  = elapsed
        reason = str(exc.reason) if hasattr(exc, 'reason') else str(exc)
        result['error'] = reason[:200]

    except Exception as exc:
        elapsed = int((time.time() - t0) * 1000)
        result['state']             = 'ERROR'
        result['response_time_ms']  = elapsed
        result['error']             = str(exc)[:200]

    return result


# ── Bulk check in parallel ────────────────────────────────────────────────────

def check_all_apis(vault_client, server_cfg, api_names=None):
    """
    Check all (or a subset of) APIs in parallel using threads.

    vault_client: VaultClient instance
    server_cfg:   dict for the current server
    api_names:    optional list of API names to filter; None = all

    Returns list of result dicts.
    """
    secret_path = server_cfg.get('vault_secret_path', '')
    ssl_ctx_obj = vault_client._ssl if vault_client else None

    # Fetch Vault secrets (cached 5 min)
    try:
        secrets = vault_client.get_secret(secret_path) if (vault_client and secret_path) else {}
    except Exception as exc:
        error_msg = 'Vault error: {0}'.format(exc)
        return [{
            'name':             'Vault',
            'state':            'ERROR',
            'status_code':      None,
            'response_time_ms': 0,
            'team':             '',
            'contact':          '',
            'error':            error_msg,
        }]

    apis = get_apis()
    if api_names:
        apis = [a for a in apis if a.get('name') in api_names]

    results = [None] * len(apis)
    threads = []

    def worker(index, api_cfg):
        try:
            results[index] = check_api(api_cfg, secrets, ssl_ctx_obj, server_cfg)
        except Exception as exc:
            results[index] = {
                'name':             api_cfg.get('name', 'Unknown API'),
                'state':            'ERROR',
                'status_code':      None,
                'response_time_ms': 0,
                'team':             api_cfg.get('team', ''),
                'contact':          api_cfg.get('contact', ''),
                'error':            str(exc)[:200],
            }

    for i, api_cfg in enumerate(apis):
        t = threading.Thread(target=worker, args=(i, api_cfg))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    return results


def check_flow_ready(vault_client, server_cfg):
    """
    Check only the APIs that are part of the core flow.
    """
    flow_api_names = get_flow_check_api_names()
    return check_all_apis(vault_client, server_cfg, api_names=flow_api_names or None)


# ── Alias/name matching (for chat intent) ────────────────────────────────────

def find_api_by_text(text):
    """
    Return the first API config whose name or alias appears in text.
    """
    lower = text.lower()
    for api_cfg in get_apis():
        if api_cfg.get('name', '').lower() in lower:
            return api_cfg
        for alias in api_cfg.get('aliases', []):
            if alias.lower() in lower:
                return api_cfg
    return None
