"""Personal practice only: the participant's local model chooses each move."""

import os
import time
import uuid
from typing import Any

import httpx


class PracticeClient:
    def __init__(self, url=None, token=None, *, transport=None, timeout=5):
        self.http = httpx.Client(
            base_url=url or os.environ.get("STOCKFISH_API_URL", "http://127.0.0.1:8003"),
            headers={"Authorization": "Bearer " + (token or os.environ["STOCKFISH_TEAM_TOKEN"])},
            transport=transport,
            timeout=timeout,
            trust_env=False,
        )

    def _request(self, method, path, **kwargs) -> dict[str, Any]:
        # All mutations carry a stable request/action ID. A lost response may be retried safely.
        for attempt in range(3):
            try:
                response = self.http.request(method, path, **kwargs)
                if response.status_code == 429 and attempt < 2:
                    time.sleep(1)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Practice API response must be a JSON object")
                return payload
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
        raise RuntimeError("Practice request retries exhausted")

    def profiles(self):
        return self._request("GET", "/v1/profiles")

    def create(
        self,
        *,
        colour="random",
        level=5,
        white_seconds=600,
        black_seconds=600,
        increment=0,
        agent=None,
        request_id=None,
    ):
        return self._request(
            "POST",
            "/v1/sessions",
            json={
                "request_id": request_id or uuid.uuid4().hex,
                "colour": colour,
                "level": level,
                "time_control": {
                    "white": white_seconds,
                    "black": black_seconds,
                    "increment": increment,
                },
                "agent": agent or {},
            },
        )

    def read(self, session_id):
        return self._request("GET", f"/v1/sessions/{session_id}")

    def action(self, session, *, action="move", uci=None, action_id=None):
        data = {
            "action_id": action_id or uuid.uuid4().hex,
            "position_version": session["position_version"],
            "action": action,
        }
        if uci is not None:
            data["uci"] = uci
        return self._request("POST", f"/v1/sessions/{session['id']}/actions", json=data)

    def move(self, session, uci, *, action_id=None):
        return self.action(session, uci=uci, action_id=action_id)

    def resign(self, session, *, action_id=None):
        return self.action(session, action="resign", action_id=action_id)

    def wait(self, session_id, *, timeout=120, poll=0.1):
        deadline = time.monotonic() + timeout
        while True:
            session = self.read(session_id)
            if not session["engine_pending"] or session["status"] != "running":
                return session
            if time.monotonic() >= deadline:
                raise TimeoutError("Engine response pending; reconnect with the same session ID")
            time.sleep(poll)

    def history(self, *, limit=50, offset=0):
        return self._request(
            "GET", "/v1/teams/me/history", params={"limit": limit, "offset": offset}
        )

    def pgn(self, session_id):
        response = self.http.get(f"/v1/sessions/{session_id}/pgn")
        response.raise_for_status()
        return response.text

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
