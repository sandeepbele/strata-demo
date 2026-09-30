"""Compatibility entry point for the PDF comparison command."""

from backend.diff.pdf import main


if __name__ == "__main__":
    raise SystemExit(main())
