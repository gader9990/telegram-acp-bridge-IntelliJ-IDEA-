"""Exercise initialize through the proxy and installed adapter, without any LLM turn."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tempfile
from bridge import Bridge
from ipc import Hub


def main():
    here = Path(__file__).resolve().parent
    configured = json.loads((here / "config.local.json").read_text(encoding="utf-8-sig"))
    # Independent lock: never interfere with an already running user bridge.
    with tempfile.TemporaryDirectory(prefix="acp-smoke-") as directory:
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({"agent_command": configured["agent_command"],
                                      "owner_id": 1, "chat_id": 1}), encoding="utf-8")
        core = Bridge(lambda _: None, lambda _: None, lambda *args: None, 1, 1)
        hub = Hub(config, core)
        hub.start()
        try:
            check(config, stdio_only=False)
        finally:
            hub.close()


def check(config, stdio_only=True):
    here = Path(__file__).resolve().parent
    command = [sys.executable, str(here / "bridge.py"), '--config', str(config)]
    if stdio_only:
        command.append('--stdio-only')
    process = subprocess.Popen(command,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    received = queue.Queue()
    def read():
        for line in process.stdout:
            received.put(json.loads(line))
        received.put(None)
    threading.Thread(target=read, daemon=True).start()
    try:
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": 1, "clientCapabilities": {},
            "clientInfo": {"name": "telegram-bridge-smoke", "version": "0.1.0"}}}
        process.stdin.write((json.dumps(request) + "\n").encode())
        process.stdin.flush()
        message = received.get(timeout=25)
        while message and message.get("id") != 1:
            message = received.get(timeout=25)
        if not message or "error" in message:
            raise RuntimeError("initialize failed")
        result = message["result"]
        if result.get("protocolVersion") != 1:
            raise RuntimeError("Unsupported protocol version")
        print("PASS: initialize through IDE relay, persistent host and installed ACP adapter")
        print("Agent:", json.dumps(result.get("agentInfo", {}), ensure_ascii=False))
        print("loadSession:", result.get("agentCapabilities", {}).get("loadSession", False))
        print("No session, prompt, model request or Telegram connection was created.")
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            raise RuntimeError("Proxy did not stop on EOF")
        if process.returncode:
            raise RuntimeError(f"Proxy exited with code {process.returncode}")


if __name__ == "__main__":
    main()
