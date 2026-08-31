# UAT Ops Chatbot

Rule-based CLI chatbot. **Zero dependencies** — stdlib only. Works on Python 3.6.8.

## Files

| File | Purpose |
|---|---|
| `app.py` | Main loop — run this |
| `intents.py` | Keyword matching, service/env extraction |
| `config.ini` | All servers, services, aliases — edit here only |

## Deploy to server

```bash
scp app.py intents.py config.ini cpndev01@cpnuatap030:/home/cpndev01/scripts/Gokul/
```

## Run

```bash
cd /home/cpndev01/scripts/Gokul
python3 app.py
```

## SSH key setup (one-time, for remote servers 036 / 027)

```bash
# Run from 030 once:
ssh-copy-id cpndev01@cpnuatap036
ssh-copy-id cpndev01@cpnuatap027
```

Without this, remote calls will fail with `Permission denied`.

## Example commands

```
is EAI up?
check all services
check all on sit
is rmq down?
restart cans
start wmq
stop rmq-producer
switch to prepro
use 036
which server am i on?
list services
help
exit
```

## Adding a new server

Just add a block to `config.ini` — no code changes:

```ini
[server:999]
host        = cpnuatap999
user        = cpndev01
scripts_dir = /home/cpndev01/scripts
services    = EAI,cans
aliases     = newenv,999
```

## Known issues from handoff

1. Hostnames for 036 and 027 are guessed (`cpnuatap036`, `cpnuatap027`) — verify and fix in `config.ini`.
2. SSH key not yet set up to 027 — run `ssh-copy-id` from 030.
3. Services assumed identical across all 3 servers — confirm per env.
