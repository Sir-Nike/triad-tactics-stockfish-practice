"""Organizer-side online practice service. Never imported by the tournament harness."""

import argparse
import hmac
import json
import math
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import chess
import chess.pgn
import httpx

# Search budgets, not native UCI Skill Level or calibrated Elo ratings.
PROFILES = [
    {
        "level": i + 1,
        "depth": depth,
        "maxThinkingTime": thinking,
        "provider": "chess-api.com",
        "strength": "search-budget preset",
    }
    for i, (depth, thinking) in enumerate(
        [
            (1, 10),
            (2, 15),
            (3, 20),
            (4, 30),
            (6, 40),
            (8, 50),
            (10, 60),
            (12, 75),
            (15, 90),
            (18, 100),
        ]
    )
]


class APIError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


class ChessAPI:
    def __init__(self, transport=None):
        self.http = httpx.Client(timeout=15, transport=transport, trust_env=False)

    def choose(self, board, profile):
        response = self.http.post(
            "https://chess-api.com/v1",
            json={
                "fen": board.fen(),
                "variants": 1,
                "depth": profile["depth"],
                "maxThinkingTime": profile["maxThinkingTime"],
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Malformed engine response")
        move = chess.Move.from_uci(data.get("move", ""))
        if move not in board.legal_moves:
            raise ValueError("Engine returned an illegal move")
        return move


@dataclass
class Game:
    id: str
    team: str
    colour: str
    profile: dict
    clocks: dict
    increment: float
    agent: dict
    board: chess.Board = field(default_factory=chess.Board)
    version: int = 0
    status: str = "running"
    result: str | None = None
    reason: str | None = None
    pending: bool = False
    started: float = 0
    actions: dict = field(default_factory=dict)


class PracticeService:
    def __init__(self, engine=None, *, now=time.monotonic, max_sessions=2000):
        self.engine = engine or ChessAPI()
        self.now = now
        self.max_sessions = max_sessions
        self.games = {}
        self.requests = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="stockfish")

    def _finish(self, game, status, result, reason):
        game.status, game.result, game.reason = status, result, reason
        game.pending = False

    def _tick(self, game):
        if game.status != "running":
            return
        current = self.now()
        side = "white" if game.board.turn else "black"
        game.clocks[side] = max(0, game.clocks[side] - (current - game.started))
        game.started = current
        if game.clocks[side] <= 0:
            winner = not game.board.turn
            result = (
                "1/2-1/2"
                if game.board.has_insufficient_material(winner)
                else ("1-0" if winner else "0-1")
            )
            self._finish(game, "finished", result, "timeout")

    def _snapshot(self, game):
        self._tick(game)
        return {
            "id": game.id,
            "colour": game.colour,
            "opponent": dict(game.profile),
            "fen": game.board.fen(),
            "history": [m.uci() for m in game.board.move_stack],
            "legal_moves": [m.uci() for m in game.board.legal_moves]
            if game.status == "running" and not game.pending
            else [],
            "clocks": dict(game.clocks),
            "time_control": {"increment": game.increment},
            "turn": "white" if game.board.turn else "black",
            "position_version": game.version,
            "engine_pending": game.pending,
            "status": game.status,
            "result": game.result,
            "reason": game.reason,
            "agent": dict(game.agent),
            "rating_update": None,
        }

    def _get(self, team, session_id):
        game = self.games.get(session_id)
        if game is None or game.team != team:
            raise APIError(404, "Session not found")
        return game

    @staticmethod
    def _identity(data, key):
        value = data.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise APIError(400, f"{key} must be a string of 1–128 characters")
        return value

    def create(self, team, data):
        request_id = self._identity(data, "request_id")
        fingerprint = json.dumps(data, sort_keys=True)
        with self.lock:
            previous = self.requests.get((team, request_id))
            if previous:
                if previous[0] != fingerprint:
                    raise APIError(409, "request_id already used with different settings")
                return self._snapshot(self.games[previous[1]])
            if len(self.games) >= self.max_sessions:
                raise APIError(503, "Practice session capacity reached; contact the organizer")
            if any(
                g.team == team and self._snapshot(g)["status"] == "running"
                for g in self.games.values()
            ):
                raise APIError(409, "Finish or resign your current session first")
            colour, level = data.get("colour", "random"), data.get("level", 5)
            if colour not in ("white", "black", "random"):
                raise APIError(400, "colour must be white, black or random")
            if type(level) is not int or not 1 <= level <= 10:
                raise APIError(400, "level must be an integer from 1 to 10")
            control = data.get("time_control", {})
            if not isinstance(control, dict):
                raise APIError(400, "time_control must be an object")
            values = {
                k: control.get(k, default)
                for k, default in (("white", 600), ("black", 600), ("increment", 0))
            }
            for key, value in values.items():
                lower, upper = (0, 60) if key == "increment" else (1, 3600)
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not lower <= value <= upper
                ):
                    raise APIError(400, f"{key} must be between {lower} and {upper} seconds")
            agent = data.get("agent", {})
            try:
                agent_json = json.dumps(agent, allow_nan=False)
            except (ValueError, TypeError):
                raise APIError(400, "agent must contain valid JSON values") from None
            if not isinstance(agent, dict) or len(agent_json) > 4096:
                raise APIError(400, "agent must be an object of at most 4096 characters")
            if colour == "random":
                colour = secrets.choice(("white", "black"))
            game = Game(
                uuid.uuid4().hex,
                team,
                colour,
                dict(PROFILES[level - 1]),
                {"white": values["white"], "black": values["black"]},
                values["increment"],
                agent,
                started=self.now(),
            )
            self.games[game.id] = game
            self.requests[team, request_id] = (fingerprint, game.id)
            if colour == "black":
                self._queue(game)
            return self._snapshot(game)

    def _push(self, game, move):
        side = "white" if game.board.turn else "black"
        game.board.push(move)
        game.clocks[side] += game.increment
        game.version += 1
        game.started = self.now()
        outcome = game.board.outcome(claim_draw=True)
        if outcome:
            self._finish(game, "finished", outcome.result(), outcome.termination.name.lower())

    def _queue(self, game):
        game.pending = True
        self.pool.submit(self._engine_turn, game.id)

    def _engine_turn(self, session_id):
        with self.lock:
            game = self.games[session_id]
            self._tick(game)
            if game.status != "running":
                return
            board, profile = game.board.copy(), dict(game.profile)
            version = game.version
        try:
            move = self.engine.choose(board, profile)
            if move not in board.legal_moves:
                raise ValueError("Engine returned an illegal move")
        except Exception:
            with self.lock:
                # A provider failure is an invalid practice attempt, never a team loss.
                if game.status == "running":
                    self._finish(game, "invalid", None, "engine_unavailable")
            return
        with self.lock:
            self._tick(game)
            if game.status != "running" or game.version != version:
                return
            game.pending = False
            self._push(game, move)

    def read(self, team, session_id):
        with self.lock:
            return self._snapshot(self._get(team, session_id))

    def action(self, team, session_id, data):
        action_id = self._identity(data, "action_id")
        fingerprint = json.dumps(data, sort_keys=True)
        with self.lock:
            game = self._get(team, session_id)
            self._tick(game)
            previous = game.actions.get(action_id)
            if previous:
                if previous[0] != fingerprint:
                    raise APIError(409, "action_id already used with different content")
                return {**previous[1], "session": self._snapshot(game)}
            if len(game.actions) >= 4096:
                raise APIError(429, "Too many actions in this session")
            action = data.get("action", "move")
            accepted, reason = False, None
            if game.status != "running":
                reason = "game_over"
            elif (
                type(data.get("position_version")) is not int
                or data["position_version"] != game.version
            ):
                reason = "stale_position"
            elif action == "resign":
                self._finish(
                    game, "finished", "0-1" if game.colour == "white" else "1-0", "resignation"
                )
                accepted = True
            elif action == "cancel":
                if game.version == 0:
                    self._finish(game, "cancelled", None, "cancelled_before_first_move")
                    accepted = True
                else:
                    reason = "cannot_cancel_started_game"
            elif action != "move":
                reason = "unknown_action"
            elif game.pending or (game.board.turn != (game.colour == "white")):
                reason = "not_your_turn"
            else:
                try:
                    move = chess.Move.from_uci(data.get("uci", ""))
                    if move not in game.board.legal_moves:
                        raise ValueError("Illegal move")
                except (ValueError, TypeError):
                    reason = "illegal_move"
                else:
                    self._push(game, move)
                    accepted = True
                    if game.status == "running":
                        self._queue(game)
            acknowledgement = {"accepted": accepted, "reason": reason, "action_id": action_id}
            game.actions[action_id] = (fingerprint, acknowledgement)
            return {**acknowledgement, "session": self._snapshot(game)}

    def history(self, team, limit=50, offset=0):
        with self.lock:
            games = [g for g in reversed(list(self.games.values())) if g.team == team]
            return {
                "sessions": [self._snapshot(g) for g in games[offset : offset + limit]],
                "total": len(games),
                "rating": None,
            }

    def pgn(self, team, session_id):
        with self.lock:
            game = self._get(team, session_id)
            self._tick(game)
            record = chess.pgn.Game.from_board(game.board)
            record.headers.update(
                {
                    "Event": "Triad Tactics personal practice",
                    "White": team if game.colour == "white" else "Stockfish",
                    "Black": team if game.colour == "black" else "Stockfish",
                    "Result": game.result or "*",
                    "Termination": game.reason or "unterminated",
                }
            )
            return str(record) + "\n"

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        if hasattr(self.engine, "http"):
            self.engine.http.close()


def make_server(service, tokens, host="127.0.0.1", port=8003):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, data, content_type="application/json"):
            body = (
                json.dumps(data, allow_nan=False).encode()
                if content_type == "application/json"
                else data.encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self):
            parts = urlsplit(self.path)
            path = parts.path
            if self.command == "GET" and path in ("/", "/docs"):
                self._send(
                    200, Path(__file__).with_name("practice_ui.html").read_text(), "text/html"
                )
                return
            if self.command == "GET" and path == "/health":
                self._send(200, {"status": "ok", "provider": "chess-api.com"})
                return
            auth = self.headers.get("Authorization", "")
            team = next(
                (
                    name
                    for token, name in tokens.items()
                    if hmac.compare_digest(auth.encode(), ("Bearer " + token).encode())
                ),
                None,
            )
            if team is None:
                raise APIError(401, "A valid practice team token is required")
            data = {}
            if self.command == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise APIError(413, "Request body must be 1–16384 bytes")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict):
                        raise ValueError()
                except (ValueError, UnicodeDecodeError):
                    raise APIError(400, "Request body must be a JSON object") from None
            if self.command == "GET" and path == "/v1/profiles":
                self._send(200, {"profiles": PROFILES, "rating_supported": False})
            elif self.command == "POST" and path == "/v1/sessions":
                self._send(201, service.create(team, data))
            elif self.command == "GET" and path == "/v1/teams/me/history":
                query = parse_qs(parts.query)
                try:
                    limit, offset = (
                        int(query.get("limit", [50])[0]),
                        int(query.get("offset", [0])[0]),
                    )
                    if not 1 <= limit <= 100 or offset < 0:
                        raise ValueError()
                except ValueError:
                    raise APIError(400, "limit must be 1–100 and offset nonnegative") from None
                self._send(200, service.history(team, limit, offset))
            else:
                segments = path.strip("/").split("/")
                if len(segments) not in (3, 4) or segments[:2] != ["v1", "sessions"]:
                    raise APIError(404, "Endpoint not found")
                session_id = segments[2]
                if self.command == "GET" and len(segments) == 3:
                    self._send(200, service.read(team, session_id))
                elif self.command == "GET" and segments[-1] == "pgn":
                    self._send(200, service.pgn(team, session_id), "application/x-chess-pgn")
                elif self.command == "POST" and segments[-1] == "actions":
                    self._send(200, service.action(team, session_id, data))
                else:
                    raise APIError(404, "Endpoint not found")

        def _handle(self):
            self.connection.settimeout(10)
            try:
                self._dispatch()
            except APIError as exc:
                self._send(exc.status, {"detail": exc.detail})
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                self.log_error("Unhandled practice request error")
                self._send(500, {"detail": "Internal practice service error"})

        do_GET = _handle
        do_POST = _handle

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(
        description="Online Stockfish practice; separate from official evaluation"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8003)
    args = parser.parse_args()
    raw = os.environ.get("STOCKFISH_PRACTICE_TOKENS")
    if raw:
        tokens = json.loads(raw)
        if (
            not isinstance(tokens, dict)
            or not tokens
            or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v
                for k, v in tokens.items()
            )
        ):
            parser.error("STOCKFISH_PRACTICE_TOKENS must map nonempty tokens to team IDs")
    else:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            parser.error("Set STOCKFISH_PRACTICE_TOKENS before binding to a public interface")
        token = secrets.token_urlsafe(24)
        tokens = {token: "local-review"}
        print(f"Local review token: {token}", flush=True)
    service = PracticeService()
    server = make_server(service, tokens, args.host, args.port)
    print(f"Practice board and API guide: http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
