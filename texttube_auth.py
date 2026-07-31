"""Compatibility entrypoint for the TextTube authorization command."""

from texttube.entrypoints.auth import main


if __name__ == "__main__":
    raise SystemExit(main())
