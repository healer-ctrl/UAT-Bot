# UAT Ops Chatbot (Phase 2)

A lightweight, zero-dependency, rule-based operations chatbot featuring dual interfaces (Interactive Command-Line and Web Browser UI). Built entirely using Python standard libraries (stdlib only) and compatible with Python 3.6.8+.

---

## 👤 Author
* **Author**: [healer-ctrl](https://github.com/healer-ctrl)

---

## 🚀 Key Features

* **Dual Interfaces**:
  * **CLI Mode (`app.py`)**: Runs directly in the terminal for fast operator console usage.
  * **Web Browser UI Mode (`server.py`)**: An elegant, responsive, dark-themed chat interface with interactive collapsible result banners and option buttons.
* **Flow-Aware Health Checks**:
  * Checks component readiness (Services + APIs) grouped by business flows (`flows.json`) including: **DTCC, Bloomberg, SWIFT, SWING, Position, Nuvo, GCopy, and Notifications**.
  * Multi-turn confirmation triggers if an environment is not specified in the flow check command.
* **Advanced API Health Monitoring (`api_checker.py`)**:
  * **Concurrent API Polling**: Multi-threaded checker runs all external API requests in parallel.
  * **Dynamic URL Templating**: Automatically resolves env-specific API URLs (e.g. CANS Actuator).
  * **Protocols**: Supports OAuth2 authenticated endpoints, open HTTP health/actuators, and direct TCP socket reachability checks (Quasar, SDS, Socrate, Demeter).
* **Multi-Host & Process Overrides (`config.ini`)**:
  * Support for service-specific target host overrides (`host.si`, `host.batch`).
  * Dedicated execution user overrides (`user.batch`) and target scripts folder path mappings.
* **Direct JVM Status Checking**:
  * Automatically resolves checks for `si` and `batch` (NAP) services by running `jps` directly on target servers and parsing process counts (`MasterThread` and `NCSAsynchronousProcessor` instances).

---

## 📂 Project Structure

| File | Description |
|---|---|
| [`app.py`](app.py) | CLI main loop execution entrypoint. |
| [`server.py`](server.py) | Web server and Browser UI backend (serves on port `8080`). |
| [`intents.py`](intents.py) | NLP parsing, intent classification, and fuzzy service matching. |
| [`api_checker.py`](api_checker.py) | Concurrent API polling engine supporting HTTP, OAuth2, and TCP socket checks. |
| [`vault_client.py`](vault_client.py) | Vault credentials client wrapper. |
| [`config.ini`](config.ini) | Environments, hosts, services, commands, and service-level overrides. |
| [`apis.json`](apis.json) | Configuration catalog for external APIs (with auth and type definitions). |
| [`flows.json`](flows.json) | Map of business flows to their respective services and APIs. |

---

## 🛠️ Setup & Running

### 1. Terminal CLI Mode
To interact with the chatbot via the terminal:
```bash
python3 app.py
```

### 2. Browser UI Mode
To boot the web application server:
```bash
python3 server.py
```
Open **`http://localhost:8080`** in your browser.

---

## 🌐 Deploy to Server (030 / JUAT)

To deploy the codebase to the UAT script directory:
```bash
scp app.py server.py intents.py api_checker.py vault_client.py config.ini apis.json flows.json cpndev01@cpnuatap030:/home/cpndev01/scripts/Gokul/
```

### Remote Server SSH Setup
The app operates on remote servers `036` and `027` via SSH. Ensure authentication keys are copied over so remote operations run seamlessly without prompt blocks:
```bash
# Execute once from 030:
ssh-copy-id cpndev01@cpnuatap036
ssh-copy-id cpndev01@cpnuatap027
```

---

## 💬 Example Chat Commands

* **Flow Checking**:
  * `"check DTCC flow in JUAT"`
  * `"is bloomberg flow ready on preprod?"`
  * `"check Position"` (triggers environment clarification prompt)
* **API Checking**:
  * `"check all apis"`
  * `"is galaxy up?"`
  * `"api status"`
* **Service Status & Lifecycle**:
  * `"is EAI up?"`
  * `"status of batch"`
  * `"restart cans on preprod"`
  * `"stop wmq-file-Integrator"`
  * `"start si"`
* **Meta**:
  * `"list services"`
  * `"help"`
  * `"which environment?"`
