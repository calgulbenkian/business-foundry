from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from . import models
from .service import Service
from .store import DomainError, Store


def backup_database(source: Path, destination: Path):
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must differ.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Source database failed its integrity check.")
            if src.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
                raise ValueError("Unsupported source database version.")
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Business Foundry: start, acquire and operate with evidence."
    )
    parser.add_argument(
        "--db", default=os.getenv("FOUNDRY_DB", ".foundry/foundry.db"), help="Private SQLite database path"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize a private database and owner key")
    init.add_argument("--name", default="Local owner")
    commands.add_parser("token", help="Print the local owner key for sign-in (keep it private)")
    commands.add_parser("demo", help="Load the explicitly synthetic Polymer demonstration")
    serve = commands.add_parser("serve", help="Start the local web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    backup = commands.add_parser("backup", help="Create an atomic SQLite backup, including access keys")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Restore into a new, absent database path")
    restore.add_argument("source", type=Path)
    create = commands.add_parser("create", help="Create a business from a JSON configuration")
    create.add_argument("configuration", type=Path)
    for name in ("forecast", "acquire"):
        entry = commands.add_parser(name, help="Save a financial model or acquisition analysis from JSON")
        entry.add_argument("business_id")
        entry.add_argument("configuration", type=Path)
    document = commands.add_parser("document", help="Generate a draft document from business records")
    document.add_argument("business_id")
    document.add_argument(
        "template",
        choices=[
            "business_plan",
            "structure_options",
            "contract_brief",
            "operating_manual",
            "readiness_report",
            "investment_memo",
        ],
    )
    document.add_argument("--deal-id")
    for name in ("export", "verify"):
        command = commands.add_parser(
            name,
            help="Export business JSON" if name == "export" else "Check the event chain and record history",
        )
        command.add_argument("business_id")
    principal = commands.add_parser(
        "key", help="Create a scoped API access key; save the printed token securely"
    )
    principal.add_argument("--name", required=True)
    principal.add_argument("--role", choices=["operator", "reviewer", "viewer", "agent"], required=True)
    principal.add_argument("--business", required=True, action="append")
    commands.add_parser("mcp", help="Run the stdio agent adapter against a running Foundry server")
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    token_path = db_path.parent / "owner.key"
    try:
        if args.command == "mcp":
            from .mcp_server import main as mcp_main

            mcp_main()
            return
        if args.command == "restore":
            backup_database(args.source, db_path)
            print(
                f"Restored {db_path}. Existing credentials remain valid; retain the original owner.key securely."
            )
            return
        store = Store(db_path)
        service = Service(store)
        if args.command in {"init", "demo", "serve"}:
            item = store.bootstrap(getattr(args, "name", "Local owner"), token_path)
            if item:
                print(f"Created private database. Owner key saved in {token_path}", file=sys.stderr)
        if args.command == "init":
            print(f"Ready. Run 'foundry token' to retrieve the sign-in key. Database: {db_path}")
        elif args.command == "token":
            print(token_path.read_text().strip())
        elif args.command == "serve":
            import uvicorn

            from .api import create_app

            print(
                f"Open http://{args.host}:{args.port}. Retrieve the sign-in key with 'foundry token'.",
                file=sys.stderr,
            )
            uvicorn.run(create_app(db_path), host=args.host, port=args.port, log_level="info")
        elif args.command == "backup":
            backup_database(db_path, args.destination)
            print(
                f"Saved {args.destination}. This private backup includes credential hashes and business data."
            )
        else:
            actor = store.authenticate(token_path.read_text().strip())
            if args.command == "demo":
                from .demo import seed

                print(json.dumps(seed(service, actor), indent=2))
            elif args.command == "key":
                print(
                    json.dumps(
                        service.add_principal(
                            actor, {"name": args.name, "role": args.role, "business_ids": args.business}
                        ),
                        indent=2,
                    )
                )
            elif args.command in {"export", "verify"}:
                result = service.audit(actor, args.business_id, args.command == "export")
                print(json.dumps(result if args.command == "export" else result["integrity"], indent=2))
                if not result["integrity"]["valid"]:
                    raise SystemExit(1)
            elif args.command == "create":
                payload = models.BusinessCreate.model_validate_json(
                    args.configuration.read_text()
                ).model_dump(mode="json")
                print(json.dumps(service.create_business(actor, str(uuid4()), payload), indent=2))
            elif args.command in {"forecast", "acquire"}:
                model = models.ForecastCreate if args.command == "forecast" else models.DealInputs
                payload = model.model_validate_json(args.configuration.read_text()).model_dump(mode="json")
                fn = service.save_forecast if args.command == "forecast" else service.save_deal
                print(json.dumps(fn(actor, args.business_id, str(uuid4()), payload), indent=2))
            elif args.command == "document":
                payload = models.DocumentCreate(template=args.template, deal_id=args.deal_id).model_dump(
                    mode="json"
                )
                print(
                    json.dumps(
                        service.generate_document(actor, args.business_id, str(uuid4()), payload), indent=2
                    )
                )
    except (DomainError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Foundry: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
