"""CLI entry point; application commands are added at integration time."""
import argparse

from evo import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evolutionary multi-agent platform")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)
    parser.print_help()
    return 0
