"""Pipe-based inter-Ralph communication.

Layout in BUS_DIR/:
  <id>.fifo        — POSIX named pipe; other Ralphs write request_ids here (line-delimited).
  <id>.status      — JSON status file, atomically rewritten by this Ralph each iteration.
  req/<rid>.json   — request payload {request_id, from, to, category, description, ts}.
                    `description` is the requester's short phrase describing what they need;
                    the receiver uses it to pick the most relevant tool/lesson to return.
  resp/<rid>.json  — response payload {request_id, from, answer, ts}. answer is shaped by
                    the receiver's fulfill_request callback (typically {files: {...}} for
                    "tools" requests or {lessons: [...]} for memory categories).
"""
import json
import os
import select
import threading
import time
import uuid
from pathlib import Path


class Bus:
    def __init__(self, bus_dir: Path, ralph_id: str, fulfill_request=None):
        """fulfill_request(category: str, request: dict) -> dict — called on the listener
        thread when an ask arrives. If set, the listener auto-responds with its return
        value. If None, requests are queued in _pending for drain_pending() (test path).
        """
        self.bus_dir = Path(bus_dir)
        self.id = ralph_id
        self.fulfill_request = fulfill_request
        self.bus_dir.mkdir(parents=True, exist_ok=True)
        (self.bus_dir / "req").mkdir(exist_ok=True)
        (self.bus_dir / "resp").mkdir(exist_ok=True)
        self.fifo_path = self.bus_dir / f"{ralph_id}.fifo"
        if not self.fifo_path.exists():
            os.mkfifo(self.fifo_path)
        # Hold both ends ourselves so the read fd never sees EOF when no other writer is connected.
        self._read_fd = os.open(str(self.fifo_path), os.O_RDONLY | os.O_NONBLOCK)
        self._writer_fd = os.open(str(self.fifo_path), os.O_WRONLY | os.O_NONBLOCK)
        self._buffer = b""
        self._pending: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._peek_cache: dict[str, tuple[float, dict]] = {}  # ralph_id -> (mtime_ns, parsed)
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def _listen(self) -> None:
        while not self._stop.is_set():
            ready, _, _ = select.select([self._read_fd], [], [], 0.2)
            if not ready:
                continue
            try:
                data = os.read(self._read_fd, 4096)
            except OSError:
                continue
            if not data:
                time.sleep(0.05)
                continue
            self._buffer += data
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                rid = line.decode(errors="replace").strip()
                if not rid:
                    continue
                req = self.bus_dir / "req" / f"{rid}.json"
                if not req.exists():
                    continue
                try:
                    payload = json.loads(req.read_text())
                except Exception:
                    continue
                if self.fulfill_request is not None:
                    try:
                        answer = self.fulfill_request(payload.get("category", ""), payload)
                    except Exception as e:
                        answer = {"error": f"fulfillment failed: {type(e).__name__}: {e}"}
                    self.respond(payload["request_id"], answer)
                    print(f"  📤 fulfilled {payload.get('category')!r} request from {payload.get('from')}")
                else:
                    with self._lock:
                        self._pending.append(payload)

    def drain_pending(self) -> list[dict]:
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
        return items

    def write_status(self, status: dict) -> None:
        path = self.bus_dir / f"{self.id}.status"
        tmp = path.with_suffix(".status.tmp")
        tmp.write_text(json.dumps(status))
        tmp.rename(path)

    def list_ralphs(self) -> list[str]:
        return sorted(p.stem for p in self.bus_dir.glob("*.status"))

    def peek_ralph(self, ralph_id: str) -> dict:
        path = self.bus_dir / f"{ralph_id}.status"
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            self._peek_cache.pop(ralph_id, None)
            return {"error": f"no status for ralph: {ralph_id}"}
        cached = self._peek_cache.get(ralph_id)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            parsed = json.loads(path.read_text())
            self._peek_cache[ralph_id] = (mtime, parsed)
            return parsed
        except Exception as e:
            return {"error": f"unreadable status: {e}"}

    def ask(self, target: str, category: str, description: str = "") -> dict:
        target_fifo = self.bus_dir / f"{target}.fifo"
        if not target_fifo.exists():
            return {"error": f"target ralph not running: {target}"}
        request_id = uuid.uuid4().hex[:12]
        payload = {
            "request_id": request_id,
            "from": self.id,
            "to": target,
            "category": category,
            "description": description,
            "ts": time.time(),
        }
        (self.bus_dir / "req" / f"{request_id}.json").write_text(json.dumps(payload))
        try:
            fd = os.open(str(target_fifo), os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            return {"error": f"target unreachable: {e}"}
        try:
            os.write(fd, f"{request_id}\n".encode())
        finally:
            os.close(fd)
        return {"request_id": request_id, "status": "sent"}

    def check_response(self, request_id: str) -> dict:
        resp_path = self.bus_dir / "resp" / f"{request_id}.json"
        if not resp_path.exists():
            return {"request_id": request_id, "status": "pending"}
        return json.loads(resp_path.read_text())

    def respond(self, request_id: str, answer) -> dict:
        """`answer` may be a string or a JSON-able dict — both are written verbatim."""
        resp_path = self.bus_dir / "resp" / f"{request_id}.json"
        resp_path.write_text(json.dumps({
            "request_id": request_id,
            "from": self.id,
            "answer": answer,
            "ts": time.time(),
        }))
        return {"status": "responded", "request_id": request_id}

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for fd in (self._writer_fd, self._read_fd):
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            self.fifo_path.unlink()
        except Exception:
            pass
