"""
intents.py — Intent classification, service/env extraction.
stdlib only.  Python 3.6+ compatible.
"""

import re
import difflib

# ── Intent example phrases ────────────────────────────────────────────────────
# Order matters: restart is checked BEFORE start/stop to avoid "start" inside
# "restart" being a false positive.

_RESTART_PHRASES = [
    'restart', 'reboot', 'bounce', 'reload',
    'stop and start', 'stop and then start',
]

_START_PHRASES = [
    'start', 'bring up', 'boot', 'launch', 'spin up',
    'wake up', 'turn on', 'fire up', 'enable', 'bring back',
]

_STOP_PHRASES = [
    'stop', 'kill', 'shut down', 'shutdown', 'turn off',
    'disable', 'bring down', 'halt', 'terminate', 'take down',
]

_STATUS_PHRASES = [
    'is up', 'is running', 'is down', 'is it up', 'is it running',
    'check status', 'status of', 'status', 'check', 'health',
    'is working', 'is alive', 'is fine', 'is everything fine',
    'everything up', 'all up', 'all running', 'are all', 'is all',
    'check all', 'all services', 'ping',
]

# ── "All services" trigger words ──────────────────────────────────────────────
_ALL_KEYWORDS = [
    'all services', 'all apps', 'every service', 'every app',
    'all of them', 'each service', 'everything',
    # bare 'all' only if NOT preceded by a word that makes it partial
    # (handled separately below)
]


def is_all_request(text):
    """
    Return True if user wants to act on ALL services.
    Uses explicit keyword list — NOT fuzzy — to prevent false positives
    on normal single-service queries like "is cans all good?".
    """
    lower = text.lower()
    for kw in _ALL_KEYWORDS:
        if kw in lower:
            return True
    # bare 'all' as a standalone token
    tokens = re.findall(r'[a-z0-9_-]+', lower)
    if 'all' in tokens:
        # Don't trigger if 'all' is next to a specific service name fragment
        # Simple heuristic: if the sentence has no OTHER known-service word
        # around it, treat 'all' as "all services"
        return True
    return False


def classify_intent(text):
    """
    Return one of: 'restart', 'start', 'stop', 'status'.
    Checks restart first (contains the word 'start' inside it).
    Falls back to 'status' when nothing matches.
    """
    lower = text.lower()

    for phrase in _RESTART_PHRASES:
        if phrase in lower:
            return 'restart'

    for phrase in _START_PHRASES:
        if phrase in lower:
            return 'start'

    for phrase in _STOP_PHRASES:
        if phrase in lower:
            return 'stop'

    return 'status'


def extract_service(text, services_on_server, service_aliases):
    """
    Return the canonical service name if found in text, else None.
    Checks aliases first, then direct service name substring match.
    Case-insensitive.
    """
    lower = text.lower()

    # 1. Alias match (short forms like 'rmq', 'wmq', 'eai')
    for alias, canonical in service_aliases.items():
        # whole-word alias check to avoid 'eai' hitting 'heais' etc.
        if re.search(r'\b' + re.escape(alias.lower()) + r'\b', lower):
            if canonical in services_on_server:
                return canonical

    # 2. Direct case-insensitive substring match
    for svc in services_on_server:
        if svc.lower() in lower:
            return svc

    return None


def suggest_services(text, all_services, service_aliases):
    """
    Fuzzy fallback: return a list of possible service names the user might mean.
    Used for the "Did you mean...?" disambiguation prompt.
    """
    lower = text.lower()
    matches = []

    # Alias lookup across ALL services (not just current server)
    for alias, canonical in service_aliases.items():
        if re.search(r'\b' + re.escape(alias.lower()) + r'\b', lower):
            if canonical in all_services and canonical not in matches:
                matches.append(canonical)

    if matches:
        return matches

    # Fuzzy match individual tokens against service names
    tokens = re.findall(r'[a-z0-9_-]+', lower)
    svc_lower_map = {s.lower(): s for s in all_services}
    for token in tokens:
        close = difflib.get_close_matches(token, list(svc_lower_map.keys()), n=3, cutoff=0.65)
        for c in close:
            original = svc_lower_map[c]
            if original not in matches:
                matches.append(original)

    return matches


def extract_env(text, server_configs):
    """
    Return server key (e.g. '030', '036', '027') if a server alias is found
    in text as a whole word/token.  Whole-word matching prevents 'juat'
    from matching 'uat', etc.
    """
    tokens = set(re.findall(r'[a-z0-9_-]+', text.lower()))

    for server_key, config in server_configs.items():
        raw_aliases = config.get('aliases', '')
        aliases = [a.strip().lower() for a in raw_aliases.split(',') if a.strip()]
        for alias in aliases:
            # alias may itself be multi-word (e.g. 'pre prod') — skip those
            if ' ' in alias:
                if alias in text.lower():
                    return server_key
            else:
                if alias in tokens:
                    return server_key

    return None
