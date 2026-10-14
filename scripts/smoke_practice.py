"""Run against a local server with the real chess-api.com provider."""

import json
import os
from pathlib import Path

from stockfish_practice import PracticeClient


def main():
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    checks = []
    with PracticeClient(timeout=20) as api:
        for colour, level, white, black in [("white", 1, 600, 600), ("black", 10, 300, 180)]:
            game = api.create(
                colour=colour,
                level=level,
                white_seconds=white,
                black_seconds=black,
                agent={"harness": "live-provider-smoke-test"},
            )
            try:
                if colour == "white":
                    reply = api.move(game, "e2e4")
                    assert reply["accepted"], reply
                game = api.wait(game["id"], timeout=40)
                assert game["status"] == "running", game
                assert game["turn"] == colour and game["legal_moves"], game
                assert len(game["history"]) == (2 if colour == "white" else 1), game
                checks.append(
                    {
                        "colour": colour,
                        "level": level,
                        "history": game["history"],
                        "clocks": game["clocks"],
                        "provider_reply": "legal",
                        "session": game["id"],
                    }
                )
            finally:
                game = api.read(game["id"])
                if game["status"] == "running":
                    api.resign(game)
                (reports / f"smoke-{colour}.pgn").write_text(api.pgn(game["id"]))
        report = {
            "passed": True,
            "url": os.getenv("STOCKFISH_API_URL", "http://127.0.0.1:8003"),
            "checks": checks,
        }
        (reports / "practice-live-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
