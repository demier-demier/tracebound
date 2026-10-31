#!/usr/bin/env python3
"""Run the private loopback-only Tracebound investigation workspace."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workspace.service import Workspace, create_config, load_config, make_server  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-config", type=Path, help="Create a new 0600 private actor-token file, then exit")
    parser.add_argument("--config", type=Path, help="Private actor-token configuration (0600)")
    parser.add_argument("--database", type=Path, help="Private SQLite database path outside the public repository")
    parser.add_argument("--rust-bin", type=Path, default=ROOT / "target/release/tracebound")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--static-root", type=Path, default=ROOT / "web")
    args = parser.parse_args()
    if args.init_config:
        try:
            args.init_config.resolve().relative_to(ROOT)
        except ValueError:
            pass
        else:
            parser.error("Private configuration must be created outside the public repository")
        create_config(args.init_config)
        print("Private actor configuration created. Read and distribute tokens locally; do not publish the file.")
        return
    if not args.config or not args.database:
        parser.error("--config and --database are required; keep both outside the public repository")
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    for private in (args.config.absolute(), args.database.absolute()):
        try:
            private.resolve().relative_to(ROOT)
        except ValueError:
            pass
        else:
            parser.error("Private configuration and database must be outside the public repository")
    service = Workspace(args.database, args.rust_bin)
    server = make_server(service, load_config(args.config), port=args.port, static_root=args.static_root)
    print(f"Tracebound local workspace: http://127.0.0.1:{server.server_port}/review.html", flush=True)
    print("Single-machine evaluation scope; no SSO, shared-host isolation, or production banking controls.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
