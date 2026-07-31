"""Docker entrypoint for the unified TextTube service."""

from texttube.entrypoints.service import main


if __name__ == "__main__":
    raise SystemExit(main())
