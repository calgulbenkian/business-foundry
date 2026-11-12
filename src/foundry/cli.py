from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import get_args
from uuid import uuid4

from . import models
from .service import Service
from .store import DomainError, Store


def not_foundry_database(source: Path) -> str:
    return f"{source.resolve()} is not a Foundry database."


def backup_database(source: Path, destination: Path) -> int:
    """Copy the Foundry database at ``source`` to the new file ``destination``.

    The source database file is opened read-only and never created or changed; SQLite may add its
    own ``-wal`` and ``-shm`` working files beside a source in WAL mode. A source that is a folder, is
    not an SQLite database, or has no Foundry tables is refused, and the destination file is
    removed. Returns the number of businesses in the finished copy, counted from the copy.
    """
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must differ.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        if source.is_dir():
            raise ValueError(not_foundry_database(source))
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        try:
            try:
                check = src.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                if exc.sqlite_errorcode == sqlite3.SQLITE_NOTADB:
                    raise ValueError(not_foundry_database(source)) from None
                raise
            if check != "ok":
                raise ValueError("Source database failed its integrity check.")
            tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"metadata", "businesses"} <= tables:
                raise ValueError(not_foundry_database(source))
            try:
                version = src.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            except sqlite3.OperationalError:  # a metadata table that is not Foundry's
                version = None
            if version is None:
                raise ValueError(not_foundry_database(source))
            if version[0] != "1":
                raise ValueError("Unsupported source database version.")
            src.backup(dst)
            return dst.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
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
    jurisdiction = commands.add_parser(
        "jurisdiction",
        help="Change the planning jurisdiction as the local owner: supersede the old guide's steps, "
        "add the new guide's steps and reopen completed work (not legal advice; no entity is moved)",
    )
    jurisdiction.add_argument("business_id")
    jurisdiction.add_argument("jurisdiction", choices=get_args(models.Jurisdiction))
    jurisdiction.add_argument("--reason", help="Why the human decision was made (required to change)")
    jurisdiction.add_argument(
        "--evidence", action="extend", nargs="+", default=[], metavar="ID", help="Reviewed evidence IDs"
    )
    jurisdiction.add_argument(
        "--expected-version", type=int, help="Business version the decision was based on (default: current)"
    )
    jurisdiction.add_argument(
        "--preview", action="store_true", help="Show the effect without changing anything"
    )
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
        if args.command == "backup":
            # Checked before anything is opened: naming no database must not create one.
            if not db_path.exists():
                raise ValueError(f"No database at {db_path}. Name the database to back up with --db.")
            # Folders this backup creates for its destination are removed again if it fails.
            created, folder = [], args.destination.resolve().parent
            while not folder.exists() and folder != folder.parent:
                created.append(folder)
                folder = folder.parent
            try:
                count = backup_database(db_path, args.destination)
            except BaseException:
                for folder in created:  # the deepest first; a folder that is not empty stays
                    with contextlib.suppress(OSError):
                        folder.rmdir()
                raise
            print(
                f"Saved {args.destination.resolve()}: a copy of {db_path} holding {count} "
                f"{'business' if count == 1 else 'businesses'}. "
                "This private backup includes credential hashes and business data."
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
            elif args.command == "jurisdiction":
                if args.preview:
                    result = service.preview_jurisdiction(actor, args.business_id, args.jurisdiction)
                else:
                    if args.reason is None:
                        raise ValueError("Give --reason for the jurisdiction decision, or use --preview.")
                    version = args.expected_version
                    if version is None:
                        version = service.snapshot(actor, args.business_id)["business"]["version"]
                    payload = models.JurisdictionChange(
                        expected_version=version,
                        jurisdiction=args.jurisdiction,
                        reason=args.reason,
                        evidence_ids=args.evidence,
                    ).model_dump(mode="json")
                    result = service.change_jurisdiction(actor, args.business_id, str(uuid4()), payload)
                print(json.dumps(result, indent=2))
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
