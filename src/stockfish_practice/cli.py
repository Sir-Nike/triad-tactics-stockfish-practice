"""Command line runner for a participant's own practice LLM."""

import argparse
import importlib
import time

from .client import PracticeClient


def main():
    parser = argparse.ArgumentParser(description="Play your local LLM against Stockfish practice")
    parser.add_argument("--url", help="Practice API URL; defaults to STOCKFISH_API_URL")
    parser.add_argument("--session", help="Reconnect to a session ID")
    parser.add_argument("--agent", help="MODULE:CALLABLE taking a session and returning a UCI move")
    parser.add_argument(
        "--colour", "--color", choices=["white", "black", "random"], default="random"
    )
    parser.add_argument("--level", type=int, choices=range(1, 11), default=5)
    parser.add_argument("--white-minutes", type=float, default=10)
    parser.add_argument("--black-minutes", type=float, default=10)
    parser.add_argument("--increment", type=float, default=0)
    args = parser.parse_args()
    choose = None
    if args.agent:
        module_name, separator, function_name = args.agent.partition(":")
        if not separator:
            parser.error("--agent must be MODULE:CALLABLE")
        choose = getattr(importlib.import_module(module_name), function_name)
    with PracticeClient(url=args.url) as client:
        game = (
            client.read(args.session)
            if args.session
            else client.create(
                colour=args.colour,
                level=args.level,
                white_seconds=args.white_minutes * 60,
                black_seconds=args.black_minutes * 60,
                increment=args.increment,
                agent={"harness": args.agent or "interactive"},
            )
        )
        print(f"Session {game['id']}; playing {game['colour']}; level {game['opponent']['level']}")
        try:
            while game["status"] == "running":
                game = client.wait(game["id"])
                if game["status"] != "running":
                    break
                uci = (
                    choose(game)
                    if choose
                    else input(f"{game['fen']}\nUCI move (or resign): ").strip()
                )
                if not isinstance(uci, str):
                    raise TypeError("Local practice callback must return a UCI string or 'resign'")
                response = client.resign(game) if uci == "resign" else client.move(game, uci)
                game = response["session"]
                if not response["accepted"]:
                    print(f"Rejected: {response['reason']}")
                time.sleep(0.05)
        except KeyboardInterrupt:
            print(f"Disconnected; clock continues. Reconnect with --session {game['id']}")
            return
        print(f"{game['status']}: {game['result']} ({game['reason']})")


if __name__ == "__main__":
    main()
