"""Provision the LAMF pipeline dashboard on the shared Metabase instance.

Same rationale as Cadence's ``scripts/provision_metabase.py``: a card built in
the Metabase UI lives only in Metabase's own application database, so it is not
version controlled and cannot be reviewed or diffed. The definition instead
lives in ``sql/dashboard_questions.sql`` and this file, and the instance is
rebuilt from them.

Bridge does **not** create its own database connection. It shares Postgres with
Cadence, and Cadence's provisioning script already registers that connection
under the name given by ``METABASE_DATABASE_NAME`` (default ``"Cadence"``).
Creating a second connection to the same database would let the two dashboards'
schema caches drift out of sync for no reason — this script looks up the
existing connection and adds a second dashboard against it.

Run against an already-provisioned instance:

    docker compose up -d metabase   # if not already running
    python -m scripts.provision_metabase

Idempotent: re-running updates existing cards rather than duplicating them.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from src.db import PROJECT_ROOT

logger = logging.getLogger("bridge.metabase")

QUESTIONS_PATH = PROJECT_ROOT / "sql" / "dashboard_questions.sql"

DASHBOARD_NAME = "Bridge — LAMF Pipeline"
DASHBOARD_DESCRIPTION = (
    "LAMF cross-sell pipeline health: funnel, disbursal rate against the assumed "
    "band, loan book composition, the nudge's effect on SIP breakage, and the "
    "collateral constraint that governs whether any of it is viable."
)

CARD_DISPLAY = {
    1: "bar",
    2: "line",
    3: "bar",
    4: "bar",
    5: "bar",
    6: "table",
    7: "bar",
    8: "table",
}

# Grid is 24 columns wide. (col, row, size_x, size_y) per card.
CARD_LAYOUT = {
    1: (0, 0, 24, 6),
    2: (0, 6, 24, 5),
    3: (0, 11, 12, 5),
    4: (12, 11, 12, 5),
    5: (0, 16, 12, 5),
    6: (12, 16, 12, 5),
    7: (0, 21, 12, 5),
    8: (12, 21, 12, 5),
}


class MetabaseClient:
    """Thin Metabase API client — session auth and JSON in, JSON out.

    Deliberately duplicated from Cadence's client rather than imported across
    repos: Bridge and Cadence are separate packages with separate virtualenvs,
    so a cross-repo import would need one to depend on the other's filesystem
    layout. The class is ~30 lines and has no state beyond a `requests.Session`,
    which is a smaller liability than that coupling.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, self._url(path), timeout=60, **kwargs)
        if not response.ok:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        return response.json() if response.content else None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, payload: dict | None = None) -> Any:
        return self.request("POST", path, json=payload or {})

    def put(self, path: str, payload: dict | None = None) -> Any:
        return self.request("PUT", path, json=payload or {})

    def wait_until_healthy(self, timeout: int = 300) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.get("health").get("status") == "ok":
                    logger.info("metabase is healthy")
                    return
            except Exception:  # noqa: BLE001 — any failure here just means "not up yet"
                pass
            time.sleep(5)
        raise TimeoutError(f"metabase did not become healthy within {timeout}s")

    def authenticate(self, email: str, password: str) -> None:
        self.post("session", {"username": email, "password": password})
        logger.info("authenticated as %s", email)


def parse_questions(path: Path) -> dict[int, tuple[str, str]]:
    """Split ``dashboard_questions.sql`` into ``{number: (title, sql)}``."""
    text = path.read_text()
    pattern = re.compile(r"^-- CARD (\d+) — (.+?)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        raise ValueError(f"no '-- CARD n — title' headers found in {path}")

    questions: dict[int, tuple[str, str]] = {}
    for index, match in enumerate(matches):
        number = int(match.group(1))
        title = match.group(2).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)

        body = text[start:end]
        sql = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("--")
        ).strip()
        if sql:
            questions[number] = (title, sql)

    logger.info("parsed %d cards from %s", len(questions), path.name)
    return questions


def find_database_id(client: MetabaseClient, name: str) -> int:
    """Look up the shared database connection Cadence registered.

    Does NOT create one. If Cadence's provisioning has not run yet, this fails
    loudly with the fix, rather than silently registering a second connection
    to the same Postgres instance under a different name — two connections to
    one database is exactly the kind of drift this whole file exists to avoid.
    """
    payload = client.get("database")
    databases = payload["data"] if isinstance(payload, dict) else payload
    for database in databases:
        if database["name"] == name:
            logger.info("found shared database connection %d (%r)", database["id"], name)
            return int(database["id"])

    raise RuntimeError(
        f"no Metabase database connection named {name!r} was found. Bridge shares "
        "Cadence's Postgres and expects Cadence's provisioning script to have "
        "already registered it — run `python scripts/provision_metabase.py` in "
        "the Cadence repo first."
    )


def upsert_card(
    client: MetabaseClient, database_id: int, number: int, title: str, sql: str, existing: dict
) -> int:
    payload = {
        "name": title,
        "dataset_query": {
            "type": "native",
            "native": {"query": sql, "template-tags": {}},
            "database": database_id,
        },
        "display": CARD_DISPLAY.get(number, "table"),
        "visualization_settings": {},
        "description": f"Card {number} — defined in sql/dashboard_questions.sql (Bridge)",
    }

    if title in existing:
        card_id = existing[title]
        client.put(f"card/{card_id}", payload)
        logger.info("updated card %d: %s", number, title)
        return card_id

    card = client.post("card", payload)
    logger.info("created card %d: %s", number, title)
    return int(card["id"])


def upsert_dashboard(client: MetabaseClient, card_ids: dict[int, int]) -> int:
    existing = {d["name"]: d["id"] for d in client.get("dashboard")}

    if DASHBOARD_NAME in existing:
        dashboard_id = existing[DASHBOARD_NAME]
        logger.info("reusing dashboard %d", dashboard_id)
    else:
        dashboard = client.post(
            "dashboard", {"name": DASHBOARD_NAME, "description": DASHBOARD_DESCRIPTION}
        )
        dashboard_id = int(dashboard["id"])
        logger.info("created dashboard %d", dashboard_id)

    dashcards = [
        {
            "id": -number,
            "card_id": card_id,
            "col": CARD_LAYOUT.get(number, (0, 0, 12, 5))[0],
            "row": CARD_LAYOUT.get(number, (0, 0, 12, 5))[1],
            "size_x": CARD_LAYOUT.get(number, (0, 0, 12, 5))[2],
            "size_y": CARD_LAYOUT.get(number, (0, 0, 12, 5))[3],
            "parameter_mappings": [],
            "visualization_settings": {},
        }
        for number, card_id in sorted(card_ids.items())
    ]

    client.put(f"dashboard/{dashboard_id}", {"dashcards": dashcards})
    logger.info("placed %d cards on the dashboard", len(dashcards))
    return dashboard_id


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s"
    )
    parser = argparse.ArgumentParser(description="Provision the Bridge LAMF pipeline dashboard.")
    parser.add_argument(
        "--url",
        default=f"http://localhost:{os.getenv('METABASE_PORT', '3001')}",
        help="Metabase base URL",
    )
    parser.add_argument(
        "--database-name",
        default=os.getenv("METABASE_DATABASE_NAME", "Cadence"),
        help="Name of the existing Metabase database connection to build cards against",
    )
    args = parser.parse_args()

    email = os.getenv("METABASE_ADMIN_EMAIL", "admin@cadence.local")
    password = os.getenv("METABASE_ADMIN_PASSWORD")
    if not password:
        logger.error(
            "METABASE_ADMIN_PASSWORD is not set. Add it to .env — it is a real "
            "credential even on a local instance, so it is never defaulted here."
        )
        return 2

    client = MetabaseClient(args.url)
    try:
        client.wait_until_healthy()
        client.authenticate(email, password)
        database_id = find_database_id(client, args.database_name)

        questions = parse_questions(QUESTIONS_PATH)
        existing_cards = {c["name"]: c["id"] for c in client.get("card")}

        card_ids = {
            number: upsert_card(client, database_id, number, title, sql, existing_cards)
            for number, (title, sql) in sorted(questions.items())
        }
        dashboard_id = upsert_dashboard(client, card_ids)
    except Exception:
        logger.exception("provisioning failed")
        return 1

    logger.info("dashboard ready at %s/dashboard/%d", args.url, dashboard_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
