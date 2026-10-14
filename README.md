# Triad Tactics Stockfish Practice API

An organizer-run chess API and browser board where participants can try their
own local or fine-tuned language models against Stockfish. Choose a side, an
engine search level and a clock; replay games with PGN. Stockfish moves come
from the free [chess-api.com](https://chess-api.com/) service.

This service supports **personal development practice**. The TechTatva Triad
Tactics rulebook requires official Chess submissions to run offline, with the
approved organizer-served model selecting the final move. Do not include this
practice server or any external engine access in the tournament harness.

## Run the practice server

Python 3.11+ is required. From this repository:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
stockfish-practice --port 8003
```

Open **http://127.0.0.1:8003/**. The server prints a one-time token for local
review; enter it on the page. The bundled board lets a participant play by
clicking squares or entering a UCI move. It also provides reconnect and PGN
download. The API guide is at `/docs`, and `/health` is a token-free health check.

The local default binds only to loopback. To serve teams on an organizer-managed
host, supply a JSON object mapping long, random bearer tokens to registered team
IDs, then place the service behind HTTPS:

```sh
export STOCKFISH_PRACTICE_TOKENS='{"long-random-team-token":"team-a","another-long-random-token":"team-b"}'
stockfish-practice --host 0.0.0.0 --port 8003
```

Do not commit real practice tokens. Configure them in the host's secret manager.
The application checks tokens on every API request, separates sessions by team,
limits each team to one active session, and never exposes another team's history.
The server does not terminate TLS or implement account signup.

## Engine levels and clocks

The public free provider accepts search `depth` up to 18 and
`maxThinkingTime` up to 100 ms. The level selector maps to these budgets:

| Level | Search depth requested | Thinking budget requested |
| --- | ---: | ---: |
| 1 | 1 | 10 ms |
| 2 | 2 | 15 ms |
| 3 | 3 | 20 ms |
| 4 | 4 | 30 ms |
| 5 | 6 | 40 ms |
| 6 | 8 | 50 ms |
| 7 | 10 | 60 ms |
| 8 | 12 | 75 ms |
| 9 | 15 | 90 ms |
| 10 | 18 | 100 ms |

These are **search-budget presets**, not native Stockfish Skill Levels,
Elo ratings or guaranteed search depths. chess-api.com does not document a
native skill or Elo setting, and low-depth Stockfish moves can still be strong.
The provider may stop at its thinking-time budget before completing the
requested depth. Network delay is additional.
Even the lowest preset can still be much stronger at chess than a local LLM;
level 1 is the easiest available engine setting, not necessarily a balanced matchup.

Default clocks are **10 minutes each, no increment**, matching the rulebook's
main game. The browser includes an **Armageddon practice preset** with White at
5 minutes and Black at 3 minutes. The API accepts 1–3600 seconds per side and
0–60 seconds increment. It records the chess game's ordinary result; bracket
advancement and Black's draw odds remain tournament decisions.

## Test a participant's LLM

Install the participant client into the environment where the local agent runs:

```sh
python -m pip install .
export STOCKFISH_API_URL=http://127.0.0.1:8003
export STOCKFISH_TEAM_TOKEN='token-issued-by-the-organizer'
stockfish-practice-cli --agent local_agent:choose --colour black --level 5 \
  --white-minutes 10 --black-minutes 10
```

`local_agent.choose(session)` receives current FEN, move history, legal UCI moves,
clocks, participant colour, side to move, engine profile and self-reported agent
metadata. It returns a legal UCI move such as `e2e4` or `e7e8q`, or `resign`.
The callback selects the move; the service validates it and owns the board and
clocks.

Without `--agent`, the CLI takes moves interactively. Reconnect with
`stockfish-practice-cli --session SESSION_ID` using the same server URL and team
token. Disconnecting does not pause clocks or resign.

Python programs can use the client directly:

```python
from stockfish_practice import PracticeClient

with PracticeClient() as api:
    game = api.create(colour="black", level=5,
                      agent={"model": "my-local-model", "harness": "v1"})
    while game["status"] == "running":
        game = api.wait(game["id"])
        if game["status"] != "running":
            break
        response = api.move(game, your_local_agent(game))
        game = response["session"]
        if not response["accepted"]:
            print(response["reason"])
    print(game["result"], api.pgn(game["id"]))
```

The callback and practice API are for local experimentation. They do not meet
the tournament SDK's move-evidence contract. Check the event rulebook's Chess
gameplay and offline-execution sections before using the SDK for submissions.

## HTTP API

All API routes except `/` (the browser), `/docs` and `/health` require
`Authorization: Bearer TOKEN`. Requests use JSON; moves use standard UCI.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/v1/profiles` | Levels and search budgets |
| POST | `/v1/sessions` | Create a practice game |
| GET | `/v1/sessions/{id}` | Read position, legal moves and clocks |
| POST | `/v1/sessions/{id}/actions` | Submit a move, resign or cancel |
| GET | `/v1/sessions/{id}/pgn` | Download accepted moves as PGN |
| GET | `/v1/teams/me/history?limit=50&offset=0` | This token's team games |

Create a game with a client-generated `request_id`:

```json
{
  "request_id": "unique-create-id",
  "colour": "black",
  "level": 5,
  "time_control": {"white": 600, "black": 600, "increment": 0},
  "agent": {"model": "my-local-model", "harness": "v1"}
}
```

Submit each action with its own stable `action_id` and returned
`position_version`:

```json
{
  "action_id": "unique-move-id",
  "position_version": 1,
  "action": "move",
  "uci": "e7e5"
}
```

To resign or cancel, set `action` to `resign` or `cancel` and omit `uci`. Cancel
is allowed only before the first accepted move. A Black game immediately queues
White's engine opening move. Responses include an acknowledgement and the
current `session` snapshot. Poll `GET /v1/sessions/{id}` until
`engine_pending` is false before requesting another LLM move.

Retry a request after a lost response with the **same ID and JSON body**. The
service returns the original acknowledgement and current state; reusing an ID
with different content returns HTTP 409. An illegal move consumes the player's
clock. Correct using a new action ID and current position version. Threefold
repetition and the fifty-move rule are automatic draw claims.

## State, provider and operational limits

Sessions and history use in-memory storage. A restart ends games and clears
history; participants can download PGNs to retain games. Ratings are not
implemented. Up to 2000 sessions and 4096 actions per session are retained, and
four workers bound concurrent engine calls. Plan durable storage and load testing
before relying on this for a large live event.

Provider availability, latency and capacity are outside this service's control.
Each outbound request has a 15-second timeout. Provider errors, malformed replies
or illegal engine moves mark a game `invalid` with `engine_unavailable`; the
service neither picks a fallback move nor assigns the participant a loss.
Provider replies are checked against legal moves before use.

## Development

```sh
python -m pip install -e '.[dev]'
pytest -q
ruff check src tests scripts
```

For a live provider check, run the server, set `STOCKFISH_API_URL` and
`STOCKFISH_TEAM_TOKEN`, then run `python scripts/smoke_practice.py`. It plays
short test sessions as White and Black at levels 1 and 10, checks the provider
replies, resigns, and saves PGNs and a JSON report under the ignored `reports/`.
