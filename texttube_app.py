"""Compatibility entrypoint for the TextTube application command."""

from texttube.entrypoints.app import main


if __name__ == "__main__":
    raise SystemExit(main())
