"""Receiver-slot entry point for reverting a retained network transaction."""

from .networking import revert_main


if __name__ == "__main__":
    raise SystemExit(revert_main())
