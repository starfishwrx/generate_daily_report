#!/usr/bin/env python3
"""Backwards-compatible CLI and import facade for AutoDataReport."""

from autodatareport.application import *  # noqa: F403
from autodatareport.application import _870_request_fingerprint, _target_chart_input, cli_entrypoint  # noqa: F401


if __name__ == "__main__":
    cli_entrypoint()
