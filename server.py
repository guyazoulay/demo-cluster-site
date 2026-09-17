#!/usr/bin/env python3
"""Local-only web interface for creating disposable MongoDB demo clusters."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".demo-cluster-site" / "data"
VENV_DIR = Path.home() / ".demo-cluster-site" / "venv"
M_DIR = Path.home() / ".local" / "m" / "versions"
DEFAULT_PORT = 27017
LOCK = threading.Lock()
OPERATION_LOCK = threading.Lock()
STATE = {"status": "idle", "message": "Ready to create a cluster.", "logs": [], "connection": "", "topology": "", "ports": []}


def add_log(message: str) -> None:
    with LOCK:
        STATE["logs"].append(message)
        STATE["logs"] = STATE["logs"][-250:]


def run(command: list[str], *, check: bool = True, environment: dict[str, str] | None = None) -> None:
    add_log("$ " + " ".join(command))
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=os.environ | environment if environment else None,
    )
    assert process.stdout is not None
    for line in process.stdout:
        add_log(line.rstrip())
    if process.wait() and check:
        raise RuntimeError(f"Command failed: {command[0]}")


def require_macos() -> None:
    if os.uname().sysname != "Darwin":
        raise RuntimeError("This local launcher currently supports macOS only.")


def executable(name: str) -> str | None:
    return shutil.which(name)


def install_dependencies() -> Path:
    require_macos()
    if not executable("brew"):
        raise RuntimeError("Homebrew is required. Install it from https://brew.sh and retry.")

    missing = []
    if not executable("node") or not executable("npm"):
        missing.append("node")
    if not executable("mongosh"):
        missing.append("mongosh")
    if missing:
        add_log("Installing Homebrew dependencies: " + ", ".join(missing))
        run(["brew", "install", *missing])

    if not executable("m"):
        add_log("Installing MongoDB version manager (m)...")
        run(["npm", "install", "--global", "m"])

    python = VENV_DIR / "bin" / "python"
    mlaunch = VENV_DIR / "bin" / "mlaunch"
    pip = VENV_DIR / "bin" / "pip"
    if not mlaunch.exists():
        add_log("Installing mlaunch in an isolated Python environment...")
        run(["python3", "-m", "venv", str(VENV_DIR)])
        run([str(pip), "install", "--upgrade", "pip"])
    dependencies = ["mtools", "psutil", "dateutil", "pymongo", "packaging"]
    check_dependencies = [str(python), "-c", "import " + ", ".join(dependencies)]
    if subprocess.run(check_dependencies, capture_output=True).returncode:
        # mtools 1.7.2 does not declare python-dateutil in all packaging paths.
        # Repair only incomplete environments instead of reinstalling on every create.
        add_log("Installing missing Python dependencies...")
        run([str(pip), "install", "mtools", "psutil", "python-dateutil", "pymongo", "packaging"])
    return mlaunch


def number(payload: dict, key: str, minimum: int, maximum: int) -> int:
    try:
        value = int(payload[key])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{key} must be a number.") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def validate(payload: dict) -> dict:
    version = str(payload.get("version", "")).strip()
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise ValueError("Use a MongoDB version such as 7.0 or 8.0.1.")
    topology = payload.get("topology")
    if topology not in {"single", "replica", "sharded"}:
        raise ValueError("Choose a cluster topology.")
    config = {"version": version, "topology": topology, "sample_mb": number(payload, "sample_mb", 0, 500)}
    if topology == "replica":
        config["nodes"] = number(payload, "nodes", 1, 7)
        config["arbiters"] = number(payload, "arbiters", 0, 1)
        if config["arbiters"] >= config["nodes"]:
            raise ValueError("Arbiters must be fewer than replica set nodes.")
    if topology == "sharded":
        config["shards"] = number(payload, "shards", 1, 6)
        config["configs"] = number(payload, "configs", 1, 3)
        config["mongos"] = number(payload, "mongos", 1, 3)
        if config["configs"] not in {1, 3}:
            raise ValueError("Config servers must be 1 or 3.")
    return config


def additional_data_config(payload: dict) -> dict:
    collection = str(payload.get("collection", "")).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", collection):
        raise ValueError("Collection names must start with a letter or underscore and use only letters, numbers, or underscores.")
    fields = [field.strip() for field in str(payload.get("fields", "")).split(",") if field.strip()]
    if not fields:
        raise ValueError("Enter at least one field name.")
    if len(fields) != len(set(fields)) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", field) for field in fields):
        raise ValueError("Field names must be unique and use only letters, numbers, or underscores.")
    return {"collection": collection, "fields": fields, "size_mb": number(payload, "size_mb", 1, 500)}


def stop_cluster(mlaunch: Path | None = None) -> None:
    if mlaunch and DATA_DIR.exists():
        run([str(mlaunch), "stop", "--dir", str(DATA_DIR)], check=False)
    processes = demo_processes()
    if not processes:
        return
    add_log(f"Stopping {len(processes)} remaining demo MongoDB process(es)...")
    for process_id in processes:
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
    alive = wait_for_processes(processes, timeout=10)
    for process_id in alive:
        try:
            os.kill(process_id, signal.SIGKILL)
        except ProcessLookupError:
            pass
    wait_for_processes(alive, timeout=5)


def demo_processes() -> list[int]:
    data_path = str(DATA_DIR)
    processes = []
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        process_id, _, command = line.strip().partition(" ")
        if data_path in command:
            processes.append(int(process_id))
    return processes


def wait_for_processes(processes: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + timeout
    while True:
        alive = []
        for process_id in processes:
            try:
                os.kill(process_id, 0)
                alive.append(process_id)
            except ProcessLookupError:
                pass
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.1)


def ports_available(start_port: int, process_count: int) -> bool:
    for port in range(start_port, start_port + process_count):
        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                return False
    return True


def available_port_range(process_count: int) -> range:
    for start_port in range(DEFAULT_PORT, 65536 - process_count):
        if ports_available(start_port, process_count):
            return range(start_port, start_port + process_count)
    raise RuntimeError("No contiguous local port range is available for the requested cluster.")


def wait_for_primary(port: int) -> None:
    deadline = time.monotonic() + 30
    command = [
        "mongosh",
        f"mongodb://127.0.0.1:{port}/?replicaSet=replset",
        "--quiet",
        "--eval",
        "db.hello().isWritablePrimary ? 'ready' : ''",
    ]
    while time.monotonic() < deadline:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() == "ready":
            return
        time.sleep(0.1)
    raise RuntimeError("Replica set did not elect a writable primary within 30 seconds.")


def cluster_nodes(topology: str, ports: list[int]) -> list[dict]:
    nodes = [{"port": port, "role": "node", "running": False} for port in ports]
    for node in nodes:
        with socket.socket() as connection:
            node["running"] = connection.connect_ex(("127.0.0.1", node["port"])) == 0
    if topology != "replica" or not any(node["running"] for node in nodes):
        return nodes
    command = ["mongosh", f"mongodb://127.0.0.1:{ports[0]}/?replicaSet=replset", "--quiet", "--eval", "print(JSON.stringify(db.hello()))"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2)
        hello = json.loads(result.stdout) if result.returncode == 0 else {}
        primary_port = int(hello.get("primary", "localhost:0").rsplit(":", 1)[-1])
        for node in nodes:
            node["role"] = "primary" if node["port"] == primary_port else "secondary"
    except (ValueError, subprocess.TimeoutExpired):
        pass
    return nodes


def execute_shell(command: str) -> str:
    if not command.strip():
        raise ValueError("Enter a MongoDB shell command.")
    with LOCK:
        connection = STATE["connection"]
    if not connection:
        raise RuntimeError("Create a cluster before using the shell.")
    add_log(f"> {command}")
    try:
        result = subprocess.run(["mongosh", connection, "--quiet", "--eval", command], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("MongoDB shell command timed out after 15 seconds.") from error
    output = (result.stdout + result.stderr).strip() or "(no output)"
    for line in output.splitlines():
        add_log(line)
    return output


def load_sample_data(sample_mb: int, connection: str) -> None:
    if not sample_mb:
        return
    documents = sample_mb * 500
    add_log(f"Loading approximately {sample_mb} MB of sample data ({documents:,} documents)...")
    script = f"""
const collection = db.getSiblingDB('demo').getCollection('orders');
collection.drop();
const total = {documents};
for (let start = 0; start < total; start += 1000) {{
  const batch = [];
  for (let i = start; i < Math.min(start + 1000, total); i++) {{
    batch.push({{orderId: i, customer: `customer-${{i % 1000}}`, amount: (i % 500) + 0.99,
      createdAt: new Date(), tags: ['demo', `tier-${{i % 4}}`], details: {{sequence: i, source: 'demo-cluster-site'}}}});
  }}
  collection.insertMany(batch);
}}
collection.createIndex({{customer: 1, createdAt: -1}});
print(`Inserted ${{total}} documents into demo.orders.`);
"""
    run(["mongosh", connection, "--quiet", "--eval", script])


def add_collection_data(payload: dict) -> None:
    try:
        config = additional_data_config(payload)
        with LOCK:
            connection = STATE["connection"]
        if not connection:
            raise RuntimeError("Create a cluster before adding data.")
        with LOCK:
            STATE.update(status="working", message=f"Adding approximately {config['size_mb']} MB to demo.{config['collection']}...")
        add_log(f"Adding approximately {config['size_mb']} MB to demo.{config['collection']}...")
        script = f"""
const config = {json.dumps(config)};
const collection = db.getSiblingDB('demo').getCollection(config.collection);
const documents = Math.ceil(config.size_mb * 1024);
for (let start = 0; start < documents; start += 1000) {{
  const batch = [];
  for (let index = start; index < Math.min(start + 1000, documents); index++) {{
    const document = {{sequence: index, createdAt: new Date()}};
    for (let fieldIndex = 0; fieldIndex < config.fields.length; fieldIndex++) {{
      const field = config.fields[fieldIndex];
      document[field] = fieldIndex % 3 === 0 ? `${{field}}-${{index}}` : fieldIndex % 3 === 1 ? index : {{value: index, label: `${{field}}-${{index}}`}};
    }}
    document._padding = 'x'.repeat(Math.max(128, 900 - config.fields.length * 24));
    batch.push(document);
  }}
  collection.insertMany(batch);
}}
print(`Inserted ${{documents}} documents into demo.${{config.collection}}.`);
"""
        run(["mongosh", connection, "--quiet", "--eval", script])
        with LOCK:
            STATE.update(status="ready", message=f"Added data to demo.{config['collection']}.")
        add_log(f"Additional data is ready in demo.{config['collection']}.")
    except Exception as error:
        add_log(f"ERROR: {error}")
        with LOCK:
            STATE.update(status="error", message=str(error))


def create_cluster(payload: dict) -> None:
    try:
        config = validate(payload)
        with LOCK:
            STATE.update(status="working", message="Preparing local dependencies...", logs=[], connection="", topology=config["topology"], ports=[])
        mlaunch = install_dependencies()
        binary_path = M_DIR / config["version"] / "bin"
        if not binary_path.exists():
            with LOCK:
                STATE["message"] = f"Downloading MongoDB {config['version']}..."
            # m prompts before a download, but this background web operation has no stdin.
            run(["m", config["version"]], environment={"M_CONFIRM": "0"})
        if not binary_path.exists():
            raise RuntimeError(f"MongoDB {config['version']} was not installed where m expected it: {binary_path}")

        with LOCK:
            STATE["message"] = "Removing the previous demo cluster..."
        stop_cluster(mlaunch)
        process_count = 1
        if config["topology"] == "replica":
            process_count = config["nodes"] + config["arbiters"]
        elif config["topology"] == "sharded":
            process_count = config["shards"] + config["configs"] + config["mongos"]
        ports = available_port_range(process_count)
        with LOCK:
            STATE["ports"] = list(ports)
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        DATA_DIR.parent.mkdir(parents=True, exist_ok=True)

        command = [str(mlaunch), "--dir", str(DATA_DIR), "--binarypath", str(binary_path), "--port", str(ports.start)]
        if config["topology"] == "single":
            command.append("--single")
        elif config["topology"] == "replica":
            command += ["--replicaset", "--nodes", str(config["nodes"])]
            if config["arbiters"]:
                command.append("--arbiter")
        else:
            command += ["--replicaset", "--sharded", str(config["shards"]), "--config", str(config["configs"]), "--mongos", str(config["mongos"])]
        with LOCK:
            STATE["message"] = "Starting MongoDB processes..."
        run(command)
        connection = f"mongodb://127.0.0.1:{ports.start}/demo"
        if config["topology"] == "replica":
            with LOCK:
                STATE["message"] = "Waiting for a replica set primary..."
            wait_for_primary(ports.start)
            connection += "?replicaSet=replset"
        load_sample_data(config["sample_mb"], connection)
        with LOCK:
            STATE.update(status="ready", message="Cluster is ready.", connection=connection)
        add_log(f"Cluster is ready. Connect with: mongosh {connection}")
    except Exception as error:
        add_log(f"ERROR: {error}")
        with LOCK:
            STATE.update(status="error", message=str(error), connection="")


class App(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format: str, *args) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/status":
            with LOCK:
                data = STATE.copy()
            data["nodes"] = cluster_nodes(data["topology"], data["ports"])
            self.send_json(data)
            return
        if urlparse(self.path).path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in {"/api/create", "/api/add-data", "/api/stop", "/api/shell"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not OPERATION_LOCK.acquire(blocking=False):
            self.send_json({"error": "Another cluster operation is in progress."}, HTTPStatus.CONFLICT)
            return
        if route in {"/api/create", "/api/add-data", "/api/shell"}:
            try:
                size = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(size))
            except (ValueError, json.JSONDecodeError):
                OPERATION_LOCK.release()
                self.send_json({"error": "Invalid request body."}, HTTPStatus.BAD_REQUEST)
                return
            if route == "/api/create":
                threading.Thread(target=self.run_operation, args=(create_cluster, payload), daemon=True).start()
                self.send_json({"message": "Cluster creation started."}, HTTPStatus.ACCEPTED)
            elif route == "/api/add-data":
                threading.Thread(target=self.run_operation, args=(add_collection_data, payload), daemon=True).start()
                self.send_json({"message": "Additional data load started."}, HTTPStatus.ACCEPTED)
            else:
                try:
                    output = execute_shell(str(payload.get("command", "")))
                    self.send_json({"output": output})
                except (RuntimeError, ValueError) as error:
                    self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                finally:
                    OPERATION_LOCK.release()
            return
        threading.Thread(target=self.run_operation, args=(self.stop,), daemon=True).start()
        self.send_json({"message": "Stopping cluster."}, HTTPStatus.ACCEPTED)

    @staticmethod
    def run_operation(operation, *args) -> None:
        try:
            operation(*args)
        finally:
            OPERATION_LOCK.release()

    def stop(self) -> None:
        try:
            with LOCK:
                STATE.update(status="working", message="Stopping demo cluster...")
            mlaunch = VENV_DIR / "bin" / "mlaunch"
            stop_cluster(mlaunch if mlaunch.exists() else None)
            with LOCK:
                STATE.update(status="idle", message="Cluster stopped.", connection="", topology="", ports=[])
        except Exception as error:
            add_log(f"ERROR: {error}")
            with LOCK:
                STATE.update(status="error", message=str(error))


if __name__ == "__main__":
    print("Demo Cluster Site: http://127.0.0.1:8765")
    ThreadingHTTPServer(("127.0.0.1", 8765), App).serve_forever()
