import argparse

from aqla_hyperliquid_mcp.models import Settings

from .server import create_server


def main():
    parser = argparse.ArgumentParser(description="aqla Agent Google Sheets journal MCP (stdio)")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    create_server(Settings.load(args.config)).run(transport="stdio")


if __name__ == "__main__":
    main()
