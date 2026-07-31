"""Compatibility entrypoint for the TextTube scheduler command."""

from texttube.entrypoints.scheduler import main


if __name__ == "__main__":
    raise SystemExit(main())
