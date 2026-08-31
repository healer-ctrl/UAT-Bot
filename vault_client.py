#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vault_client.py  --  HashiCorp Vault client + OAuth2 token fetcher.
stdlib only. Python 3.6+. No pip.

Supports:
  - AppRole authentication (role_id + secret_id)
  - Static token authentication
  - KV v1 secret read with 5-minute in-memory cache
  - Vault namespace header (for namespaced Vault installations)
  - OAuth2 client_credentials token fetch with expiry-aware cache
"""

import json
import ssl
import time
import base64
import urllib.request
import urllib.parse
import urllib.error

# ── In-memory caches (module-level, shared across all calls) ──────────────────
_SECRET_CACHE = {}   # vault_path  → (secret_dict,  expires_epoch)
_TOKEN_CACHE  = {}   # cache_key   → (access_token, expires_epoch)


# ── SSL helper ────────────────────────────────────────────────────────────────

def _ssl_ctx(verify):
    """Return an SSL context.  verify=False disables cert checking."""
    if verify:
        return None                            # urllib uses default (system CA)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


# ── Vault client ──────────────────────────────────────────────────────────────

class VaultClient(object):
    """
    Minimal HashiCorp Vault client using urllib.request (stdlib only).

    Usage:
        vc = VaultClient.from_config(general, servers['030'])
        secrets = vc.get_secret()      # returns dict of all key-value pairs
    """

    def __init__(self, vault_url, namespace, auth_method,
                 role_id=None, secret_id=None, static_token=None,
                 kv_backend='kv', kv_version=1, verify_ssl=True):
        self.vault_url    = vault_url.rstrip('/')
        self.namespace    = namespace
        self.auth_method  = auth_method.lower().strip()
        self._role_id     = role_id
        self._secret_id   = secret_id
        self._static_tok  = static_token
        self.kv_backend   = kv_backend.strip('/')
        self.kv_version   = int(kv_version)
        self._ssl         = _ssl_ctx(verify_ssl)

        # Runtime state
        self._vault_token    = None
        self._token_expires  = 0.0

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, general_cfg, server_cfg):
        """
        Build a VaultClient from the [vault] section of config.ini and the
        vault_secret_path of a specific server section.
        """
        vcfg = general_cfg          # we pass the [vault] section dict
        verify = vcfg.get('verify_ssl', 'true').lower() not in ('false', '0', 'no')
        return cls(
            vault_url    = vcfg.get('url', ''),
            namespace    = vcfg.get('namespace', ''),
            auth_method  = vcfg.get('auth_method', 'approle'),
            role_id      = vcfg.get('role_id', ''),
            secret_id    = vcfg.get('secret_id', ''),
            static_token = vcfg.get('token', ''),
            kv_backend   = vcfg.get('kv_backend', 'kv'),
            kv_version   = int(vcfg.get('kv_version', 1)),
            verify_ssl   = verify,
        )

    # ── Internal HTTP helper ──────────────────────────────────────────────────

    def _request(self, url, data=None, extra_headers=None, timeout=15):
        """Make an HTTP request, return parsed JSON dict."""
        headers = {'Content-Type': 'application/json'}
        if self.namespace:
            headers['X-Vault-Namespace'] = self.namespace
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode('utf-8', 'replace')[:300]
            raise RuntimeError('Vault HTTP {0}: {1}'.format(exc.code, body))
        except urllib.error.URLError as exc:
            raise RuntimeError('Vault connection error: {0}'.format(exc.reason))
        except Exception as exc:
            raise RuntimeError('Vault request failed: {0}'.format(exc))

    # ── Authentication ────────────────────────────────────────────────────────

    def _get_vault_token(self):
        """Return a valid Vault token, re-authenticating if needed."""
        now = time.time()
        if self._vault_token and now < self._token_expires - 60:
            return self._vault_token

        if self.auth_method == 'token':
            if not self._static_tok:
                raise RuntimeError('[vault] token is empty in config.ini')
            self._vault_token   = self._static_tok
            self._token_expires = now + 86400       # static token: assume 24h
            return self._vault_token

        if self.auth_method == 'approle':
            if not self._role_id or self._role_id.startswith('FILL'):
                raise RuntimeError(
                    '[vault] role_id not set in config.ini. '
                    'Fill in FILL_IN_ROLE_ID_HERE with your actual AppRole role-id.')
            if not self._secret_id or self._secret_id.startswith('FILL'):
                raise RuntimeError(
                    '[vault] secret_id not set in config.ini. '
                    'Fill in FILL_IN_SECRET_ID_HERE with your actual AppRole secret-id.')

            payload = json.dumps({
                'role_id':   self._role_id,
                'secret_id': self._secret_id,
            }).encode('utf-8')

            url  = '{0}/v1/auth/approle/login'.format(self.vault_url)
            resp = self._request(url, data=payload)

            auth = resp.get('auth', {})
            tok  = auth.get('client_token', '')
            if not tok:
                raise RuntimeError('AppRole login succeeded but no client_token returned.')

            self._vault_token   = tok
            self._token_expires = now + auth.get('lease_duration', 3600)
            return self._vault_token

        raise RuntimeError('Unknown vault auth_method: {0}'.format(self.auth_method))

    # ── Secret read ───────────────────────────────────────────────────────────

    def get_secret(self, secret_path):
        """
        Read a KV secret from Vault.  Results are cached for 5 minutes.

        secret_path example:
          KV v1:  'kv/eai-juat'
          KV v2:  'kv/eai-juat'  (backend/name — the /data/ is added automatically)

        Returns a flat dict of all key-value pairs in the secret.
        """
        now = time.time()
        cached = _SECRET_CACHE.get(secret_path)
        if cached:
            data, expires = cached
            if now < expires:
                return data

        token = self._get_vault_token()

        if self.kv_version == 2:
            # KV v2: /v1/{mount}/data/{remaining_path}
            parts = secret_path.split('/', 1)
            mount = parts[0]
            name  = parts[1] if len(parts) > 1 else ''
            url   = '{0}/v1/{1}/data/{2}'.format(self.vault_url, mount, name)
        else:
            # KV v1: /v1/{full_path}
            url = '{0}/v1/{1}'.format(self.vault_url, secret_path)

        resp = self._request(url, extra_headers={'X-Vault-Token': token})

        if self.kv_version == 2:
            secret = resp.get('data', {}).get('data', {})
        else:
            secret = resp.get('data', {})

        if not isinstance(secret, dict):
            secret = {}

        _SECRET_CACHE[secret_path] = (secret, now + 300)   # cache 5 min
        return secret

    def invalidate_cache(self, secret_path=None):
        """Clear cached secrets (force re-fetch from Vault)."""
        if secret_path:
            _SECRET_CACHE.pop(secret_path, None)
        else:
            _SECRET_CACHE.clear()


# ── OAuth2 client_credentials token fetch ────────────────────────────────────

def get_oauth2_token(token_url, client_id, client_secret, scopes='', ssl_ctx=None):
    """
    Fetch an OAuth2 Bearer token using the client_credentials grant.
    Uses Basic auth (base64 clientId:clientSecret) as is standard for SGConnect.
    Tokens are cached until 30s before expiry.

    Returns the access_token string.
    Raises RuntimeError on failure.
    """
    cache_key = (token_url, client_id)
    now = time.time()

    cached = _TOKEN_CACHE.get(cache_key)
    if cached:
        tok, expires = cached
        if now < expires - 30:
            return tok

    payload = urllib.parse.urlencode({
        'grant_type': 'client_credentials',
        'scope':      scopes or '',
    }).encode('utf-8')

    creds   = '{0}:{1}'.format(client_id, client_secret)
    encoded = base64.b64encode(creds.encode('utf-8')).decode('ascii')

    req = urllib.request.Request(
        token_url,
        data    = payload,
        headers = {
            'Authorization': 'Basic {0}'.format(encoded),
            'Content-Type':  'application/x-www-form-urlencoded',
        },
    )

    try:
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        err = exc.read().decode('utf-8', 'replace')[:200]
        raise RuntimeError('OAuth2 token request failed ({0}): {1}'.format(exc.code, err))
    except Exception as exc:
        raise RuntimeError('OAuth2 token request error: {0}'.format(exc))

    tok = body.get('access_token', '')
    if not tok:
        raise RuntimeError('SGConnect returned no access_token. Response: {0}'.format(
            str(body)[:200]))

    expires_in = int(body.get('expires_in', 3600))
    _TOKEN_CACHE[cache_key] = (tok, now + expires_in)
    return tok


def clear_token_cache():
    """Force refresh of all OAuth2 tokens on next use."""
    _TOKEN_CACHE.clear()
