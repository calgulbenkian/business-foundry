import shlex
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from foundry.api import create_app
from foundry.models import BusinessCreate, EvidenceCreate, Review
from foundry.service import Service
from foundry.store import Store


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "private" / "foundry.db")
    principal = store.bootstrap()
    actor = store.authenticate(principal["token"])
    service = Service(store)
    business = service.create_business(
        actor,
        str(uuid4()),
        BusinessCreate(
            name="Test business", concept="A synthetic business for independent tests."
        ).model_dump(mode="json"),
    )
    return store, service, actor, business["id"], principal["token"]


@pytest.fixture
def client(setup):
    store, _, _, _, token = setup
    app = create_app(store.path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        yield client


def reviewed(setup, source_type="external_receipt", title="Reviewed source"):
    _, service, actor, bid, _ = setup
    evidence = service.add_evidence(
        actor,
        bid,
        str(uuid4()),
        EvidenceCreate(
            title=title,
            content="Synthetic receipt with a known original source body.",
            source_type=source_type,
        ).model_dump(mode="json"),
    )
    return service.review_evidence(
        actor,
        bid,
        evidence["id"],
        str(uuid4()),
        Review(note="Compared original source and checked relevance.", expected_version=1).model_dump(
            mode="json"
        ),
    )


FAKE_UV = """#!/bin/sh
# Stands in for uv in tests: "uv run foundry ARGS" runs this Python's foundry.cli with ARGS.
[ "$1" = run ] && [ "$2" = foundry ] || exit 97
shift 2
exec {python} -m foundry.cli "$@"
"""


def fake_uv(folder: Path) -> Path:
    """A folder holding a `uv` that runs the command line here, to put first on PATH."""
    folder.mkdir()
    (folder / "uv").write_text(FAKE_UV.format(python=shlex.quote(sys.executable)))
    (folder / "uv").chmod(0o700)
    return folder
