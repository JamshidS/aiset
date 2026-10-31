"""The `aiset` command: `aiset run <issue> ...` works a GitHub issue (aiset.run); anything else starts the office.

server.py settles the workspace from sys.argv when it's imported, so `run` is dispatched before it is.
The office takes no positional arguments, so `run` can't be mistaken for one of its options.
"""
import os
import sys

if __name__ == "__main__" and not __package__:  # run as a file: python src/aiset/cli.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    __package__ = "aiset"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "pm":
        from .launch_pm import main as pm_main
        return pm_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        from .run.cli import main as run_main
        return run_main(sys.argv[2:])
    from .server import main as serve
    serve()


if __name__ == "__main__":
    sys.exit(main())
