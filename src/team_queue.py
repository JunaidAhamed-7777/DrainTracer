"""Explicit, private, bounded upload retries; never called by a monitor."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import re
import tempfile
import time
import uuid
from pathlib import Path

from .report_contract import _check_text_depth, _unique_object
from .team_client import MAX_UPLOAD, prepare_upload
from .team_models import TeamError
from .team_security import private_file_lock, secure_directory, verify_data_file
from .team_transport import Endpoint, SharingError

MAX_QUEUE_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 64
MAX_TTL = 86400
_UUID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_RETRY = {"unavailable", "busy", "storage"}


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


class UploadQueue:
    def __init__(self, directory, project_id: str, endpoint: Endpoint, *, create=False):
        if not isinstance(project_id, str) or not _UUID.fullmatch(project_id):
            raise SharingError("invalid")
        self.directory = secure_directory(directory, create=create)
        self.path, self.lock = self.directory / "queue.json", self.directory / "queue.lock"
        self.project_id, self.endpoint = project_id, endpoint
        with private_file_lock(self.lock):
            if create and not self.path.exists():
                self._save({"queue_schema": 1, "project_id": project_id, "endpoint": self._endpoint(),
                            "observed": 0, "delivered": 0, "expired": 0, "entries": [], "history": []})
            self._load()

    def _endpoint(self):
        return {"hostname": self.endpoint.hostname, "port": self.endpoint.port, "connect_ip": self.endpoint.connect_ip}

    def _load(self):
        verify_data_file(self.path)
        with self.path.open("rb") as handle:
            raw = handle.read(MAX_QUEUE_BYTES + 1)
        if len(raw) > MAX_QUEUE_BYTES:
            raise SharingError("quota")
        try:
            text = raw.decode("utf-8")
            _check_text_depth(text)
            state = json.loads(text, object_pairs_hook=_unique_object,
                               parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
            if (not isinstance(state, dict) or set(state) != {"queue_schema", "project_id", "endpoint", "observed",
                                                            "delivered", "expired", "entries", "history"}
                    or type(state["queue_schema"]) is not int or state["queue_schema"] != 1
                    or state["project_id"] != self.project_id or state["endpoint"] != self._endpoint()
                    or any(type(state[key]) is not int or not 0 <= state[key] <= 2**63 - 1
                           for key in ("observed", "delivered", "expired"))
                    or not isinstance(state["entries"], list) or len(state["entries"]) > MAX_ENTRIES
                    or not isinstance(state["history"], list) or len(state["history"]) > MAX_ENTRIES):
                raise ValueError
            identities = set()
            for item in state["entries"]:
                if (not isinstance(item, dict) or set(item) != {"id", "digest", "report", "created_at", "expires_at",
                                                              "attempts", "next_at", "state", "last_error"}
                        or not isinstance(item["id"], str) or not _UUID.fullmatch(item["id"])
                        or item["id"] in identities or not isinstance(item["digest"], str)
                        or not _HASH.fullmatch(item["digest"]) or not isinstance(item["report"], str)
                        or len(item["report"]) > (MAX_UPLOAD + 2) // 3 * 4
                        or any(not _number(item[key]) for key in ("created_at", "expires_at", "next_at"))
                        or not 0 < item["expires_at"] - item["created_at"] <= MAX_TTL
                        or type(item["attempts"]) is not int or not 0 <= item["attempts"] <= 20
                        or item["state"] not in {"pending", "blocked"}
                        or item["last_error"] not in {None, "unavailable", "busy", "storage", "quota", "unauthorized",
                                                       "forbidden", "not_found", "invalid", "conflict", "certificate"}):
                    raise ValueError
                decoded = base64.b64decode(item["report"], validate=True)
                if len(decoded) > MAX_UPLOAD or hashlib.sha256(decoded).hexdigest() != item["digest"]:
                    raise ValueError
                identities.add(item["id"])
            for item in state["history"]:
                if (not isinstance(item, dict) or set(item) != {"id", "state", "report_id"}
                        or not isinstance(item["id"], str) or not _UUID.fullmatch(item["id"])
                        or item["state"] not in {"delivered", "expired"}
                        or (item["report_id"] is not None and (not isinstance(item["report_id"], str)
                                                              or not _UUID.fullmatch(item["report_id"])) )):
                    raise ValueError
            return state
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
            raise SharingError("invalid", "Upload queue is corrupt or does not match this project/endpoint") from error

    def _save(self, state):
        encoded = json.dumps(state, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        if len(encoded) > MAX_QUEUE_BYTES:
            raise SharingError("quota", "Upload queue reached its 32 MiB bound")
        if self.path.exists():
            verify_data_file(self.path)
        descriptor, filename = tempfile.mkstemp(prefix=".queue-", suffix=".tmp", dir=self.directory)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                verify_data_file(temporary)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _history(state, item, status, report_id=None):
        state["history"].append({"id": item["id"], "state": status, "report_id": report_id})
        state["history"] = state["history"][-MAX_ENTRIES:]

    def _expire(self, state, now):
        for item in tuple(state["entries"]):
            if now >= item["expires_at"]:
                state["entries"].remove(item)
                state["expired"] += 1
                self._history(state, item, "expired")

    def enqueue(self, raw: bytes, *, profile="share", ttl_seconds=MAX_TTL):
        if not _number(ttl_seconds) or not 1 <= ttl_seconds <= MAX_TTL:
            raise SharingError("invalid")
        prepared = prepare_upload(raw, profile=profile)
        now = time.time()
        item = {"id": uuid.uuid4().hex, "digest": hashlib.sha256(prepared).hexdigest(),
                "report": base64.b64encode(prepared).decode("ascii"), "created_at": now,
                "expires_at": now + ttl_seconds, "attempts": 0, "next_at": 0,
                "state": "pending", "last_error": None}
        with private_file_lock(self.lock):
            state = self._load()
            self._expire(state, now)
            if len(state["entries"]) >= MAX_ENTRIES:
                raise SharingError("quota", "Upload queue reached its 64-entry bound")
            state["entries"].append(item)
            state["observed"] += 1
            self._save(state)
        return {"queue_schema": 1, "id": item["id"], "state": "pending", "bytes": len(prepared)}

    def inspect(self):
        with private_file_lock(self.lock):
            state = self._load()
            self._expire(state, time.time())
            self._save(state)
            return {**state, "entries": [{key: value for key, value in item.items() if key != "report"}
                                         for item in state["entries"]]}

    def flush(self, client, *, limit=8, deadline_seconds=60, retry_blocked=False):
        if (type(limit) is not int or not 1 <= limit <= 8 or not _number(deadline_seconds)
                or not 0 < deadline_seconds <= 60 or type(retry_blocked) is not bool
                or client.project_id != self.project_id or client.endpoint != self.endpoint):
            raise SharingError("invalid")
        deadline, attempted = time.monotonic() + deadline_seconds, 0
        with private_file_lock(self.lock):
            state = self._load()
            self._expire(state, time.time())
            for item in tuple(state["entries"]):
                if attempted >= limit or time.monotonic() >= deadline:
                    break
                if item["state"] == "blocked" and not retry_blocked:
                    continue
                if item["next_at"] > time.time() and not retry_blocked:
                    continue
                attempted += 1
                item["attempts"] = min(20, item["attempts"] + 1)
                item["next_at"] = time.time() + min(3600, 2**min(item["attempts"], 12)) * random.uniform(0.75, 1.25)
                if item["attempts"] == 20:
                    item["state"] = "blocked"
                # Persist intent before an uncertain network result; same UUID
                # and identical prepared bytes survive process termination.
                self._save(state)
                try:
                    result = client.ingest_prepared(base64.b64decode(item["report"], validate=True), item["id"],
                                                    timeout=min(10, max(0.001, deadline - time.monotonic())))
                    report_id = result.get("report_id")
                    if not isinstance(report_id, str) or not _UUID.fullmatch(report_id):
                        raise SharingError("invalid")
                except (TeamError, SharingError) as error:
                    code = error.code
                    item["last_error"] = code
                    item["state"] = "pending" if code in _RETRY and item["attempts"] < 20 else "blocked"
                    item["next_at"] = time.time() + min(3600, 2**min(item["attempts"], 12)) * random.uniform(0.75, 1.25)
                else:
                    state["entries"].remove(item)
                    state["delivered"] += 1
                    self._history(state, item, "delivered", report_id)
                self._save(state)
            self._save(state)
        return {"queue_schema": 1, "attempted": attempted, "pending": len(state["entries"]),
                "delivered": state["delivered"], "expired": state["expired"], "history": state["history"]}
