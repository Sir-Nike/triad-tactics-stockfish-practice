import threading
import time
import uuid

import chess
import httpx
import pytest

from stockfish_practice import PracticeClient
from stockfish_practice.server import APIError, ChessAPI, PracticeService, make_server


class Engine:
    def __init__(self, moves=(), failure=False):
        self.moves = iter(moves)
        self.failure = failure
        self.entered = threading.Event()
        self.release = threading.Event()

    def choose(self, board, profile):
        self.entered.set()
        assert self.release.wait(2)
        if self.failure:
            raise RuntimeError("Provider unavailable")
        return chess.Move.from_uci(next(self.moves))


@pytest.fixture
def setup_service():
    services = []

    def create(moves=(), failure=False):
        engine = Engine(moves, failure)
        clock = [100.0]
        service = PracticeService(engine, now=lambda: clock[0])
        services.append(service)
        return service, engine, clock

    yield create
    for service in services:
        service.engine.release.set()
        service.close()


def create(service, **settings):
    return service.create("team", {"request_id": uuid.uuid4().hex, "colour": "white", **settings})


def action(service, game, uci=None, **settings):
    return service.action(
        "team",
        game["id"],
        {
            "action_id": uuid.uuid4().hex,
            "position_version": game["position_version"],
            "action": "move",
            "uci": uci,
            **settings,
        },
    )


def wait_engine(service, game):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = service.read("team", game["id"])
        if not current["engine_pending"]:
            return current
        time.sleep(0.005)
    pytest.fail("Engine did not finish")


def test_black_queues_first_move_and_both_clocks_account_time(setup_service):
    service, engine, clock = setup_service(["e2e4"])
    game = create(
        service, colour="black", level=10, time_control={"white": 300, "black": 180, "increment": 0}
    )
    assert game["engine_pending"] and game["legal_moves"] == []
    assert engine.entered.wait(1)
    clock[0] += 3
    engine.release.set()
    game = wait_engine(service, game)
    assert game["history"] == ["e2e4"] and game["clocks"]["white"] == 297
    clock[0] += 5
    assert service.read("team", game["id"])["clocks"]["black"] == 175


def test_move_replay_is_safe_after_engine_reply_and_settings_conflict(setup_service):
    service, engine, _ = setup_service(["e7e5"])
    data = {"request_id": "create-id", "colour": "white", "level": 3}
    game = service.create("team", data)
    assert service.create("team", data)["id"] == game["id"]
    with pytest.raises(APIError, match="different settings"):
        service.create("team", {**data, "level": 4})
    body = {"action_id": "move-id", "position_version": 0, "action": "move", "uci": "e2e4"}
    assert service.action("team", game["id"], body)["accepted"]
    engine.release.set()
    game = wait_engine(service, game)
    replay = service.action("team", game["id"], body)
    assert replay["accepted"] and replay["session"]["history"] == ["e2e4", "e7e5"]
    assert replay["session"]["position_version"] == 2
    with pytest.raises(APIError, match="different content"):
        service.action("team", game["id"], {**body, "uci": "d2d4"})
    assert not action(service, {**game, "position_version": 0}, "g1f3")["accepted"]


def test_illegal_move_consumes_clock_and_timeout_rejects_late_move(setup_service):
    service, _, clock = setup_service()
    game = create(service, time_control={"white": 10, "black": 10, "increment": 2})
    clock[0] += 4
    reply = action(service, game, "e2e5")
    assert not reply["accepted"] and reply["reason"] == "illegal_move"
    assert reply["session"]["clocks"]["white"] == 6
    clock[0] += 7
    reply = action(service, game, "e2e4")
    assert not reply["accepted"] and reply["session"]["result"] == "0-1"
    assert reply["session"]["reason"] == "timeout"


def test_increment_and_engine_failure_is_invalid(setup_service):
    service, engine, clock = setup_service(failure=True)
    game = create(service, time_control={"white": 10, "black": 10, "increment": 2})
    clock[0] += 3
    reply = action(service, game, "e2e4")
    assert reply["session"]["clocks"]["white"] == 9
    engine.release.set()
    game = wait_engine(service, game)
    assert game["status"] == "invalid" and game["result"] is None
    assert game["rating_update"] is None and game["history"] == ["e2e4"]


def test_resign_during_engine_turn_never_applies_late_reply(setup_service):
    service, engine, _ = setup_service(["e2e4"])
    game = create(service, colour="black")
    assert engine.entered.wait(1)
    reply = action(service, game, action="resign")
    assert reply["accepted"] and reply["session"]["result"] == "1-0"
    engine.release.set()
    service.pool.shutdown(wait=True)
    assert service.read("team", game["id"])["history"] == []


@pytest.mark.parametrize(
    "settings",
    [
        {"level": 0},
        {"level": True},
        {"colour": "red"},
        {"time_control": {"white": -1}},
        {"time_control": {"increment": 61}},
        {"time_control": {"black": float("nan")}},
        {"agent": []},
        {"agent": {"score": float("nan")}},
    ],
)
def test_invalid_settings(setup_service, settings):
    service, _, _ = setup_service()
    with pytest.raises(APIError):
        create(service, **settings)


def test_team_isolation_history_cancellation_and_active_limit(setup_service):
    service, _, _ = setup_service()
    game = create(service)
    with pytest.raises(APIError) as error:
        service.read("other", game["id"])
    assert error.value.status == 404
    with pytest.raises(APIError, match="current session"):
        create(service)
    assert service.history("other")["sessions"] == []
    assert action(service, game, action="cancel")["session"]["status"] == "cancelled"
    assert create(service)["status"] == "running"


@pytest.mark.parametrize(
    "fen,uci,reason",
    [
        ("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", "f7g7", "checkmate"),
        ("7k/5Q2/6K1/8/8/8/8/8 w - - 0 1", "f7e6", "stalemate"),
        ("7k/P7/8/8/8/8/8/7K w - - 0 1", "a7a8q", None),
        ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", None),
        ("7k/8/8/3pP3/8/8/8/7K w - d6 0 1", "e5d6", None),
    ],
)
def test_board_rules_and_pgn(setup_service, fen, uci, reason):
    service, _, _ = setup_service()
    game = create(service)
    service.games[game["id"]].board = chess.Board(fen)
    reply = action(service, game, uci)
    assert reply["accepted"]
    assert reply["session"]["history"] == [uci]
    if reason:
        assert reply["session"]["reason"] == reason
    assert '[Event "Triad Tactics personal practice"]' in service.pgn("team", game["id"])


def test_provider_request_uses_supported_options_and_validates_move():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"move": "e2e4" if len(requests) == 1 else "e2e5"})

    from stockfish_practice.server import PROFILES

    engine = ChessAPI(httpx.MockTransport(handle))
    assert engine.choose(chess.Board(), PROFILES[-1]).uci() == "e2e4"
    import json

    assert json.loads(requests[0].content) == {
        "fen": chess.STARTING_FEN,
        "variants": 1,
        "depth": 18,
        "maxThinkingTime": 100,
    }
    with pytest.raises(ValueError, match="illegal"):
        engine.choose(chess.Board(), PROFILES[0])
    engine.http.close()


def test_real_http_server_with_existing_practice_client(setup_service):
    service, engine, _ = setup_service(["e7e5"])
    server = make_server(service, {"test-token": "team", "other-token": "other"}, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with PracticeClient(url, "test-token") as api:
            assert len(api.profiles()["profiles"]) == 10
            game = api.create(colour="white", level=5)
            assert api.move(game, "e2e4")["accepted"]
            engine.release.set()
            game = api.wait(game["id"], timeout=2)
            assert game["history"] == ["e2e4", "e7e5"]
            assert api.resign(game)["accepted"]
            assert api.history()["total"] == 1
            assert "1. e4 e5" in api.pgn(game["id"])
        with httpx.Client(base_url=url, trust_env=False) as http:
            assert http.get("/").status_code == 200
            assert http.get("/v1/profiles").status_code == 401
            assert (
                http.get(
                    f"/v1/sessions/{game['id']}",
                    headers={
                        "Authorization": "Bearer other-token",
                    },
                ).status_code
                == 404
            )
            assert (
                http.post(
                    "/v1/sessions",
                    content="[]",
                    headers={
                        "Authorization": "Bearer test-token",
                    },
                ).status_code
                == 400
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
