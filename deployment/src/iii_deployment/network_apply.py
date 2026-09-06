"""Receiver-slot entry point for applying a retained network transaction."""

from .networking import apply_main


if __name__ == "__main__":
    raise SystemExit(apply_main())
