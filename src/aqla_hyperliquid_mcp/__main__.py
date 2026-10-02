import argparse
import json

from .coverage import audit
from .models import Settings
from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="aqla Agent Hyperliquid MCP (stdio)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--profile", choices=("agent", "full"), default="agent")
    parser.add_argument("--check-coverage", action="store_true")
    args = parser.parse_args()
    if args.check_coverage:
        result = audit()
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["complete"] else 1)
    settings = Settings.load(args.config)
    server = create_server(settings, profile=args.profile)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
