"""MIRO document type per plant (SAP Details tab -> Doc. Type). Master data: table doc_type_master.

The invoice gets `doc_type` = the document type of its plant code. A plant with one document type is automatic;
a plant with several (e.g. 2022: X4 AMBT1 / XD INMT1) or none is sent to Reconcile so a person chooses it.
The robot reads invoices.doc_type and selects it in the Details-tab drop-down."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DocTypeMaster

log = logging.getLogger("clearmatch.doctype")

# client, doc type, number range, description, plant code
SEED = """
150 X0 25 Invoice - Mundhwa 2001
150 X1 26 Invoice - R'gaon R1 2002
150 X2 27 Invoice - Chakan 2020
150 X3 28 Invoice - Khed1 2021
150 X4 29 Invoice - AMBT1 2022
150 X5 30 Invoice - AMBT2 2023
150 X6 31 Invoice - KATPHAL 2024
150 X7 32 Invoice - Bhiwadi 2025
150 X8 33 Invoice-Gujarat 2026
150 X9 34 Invoice - Khed2 2027
150 XA 35 Invoice - HT Rngn 2081
150 XD 71 Invoice - INMT1 2022
150 XE 72 Invoice - YIPL1 2301
150 XG 81 Invoice - AMBT3 2028
150 XH 82 Invoice - 2029 2029
150 XI 83 Invoice - 2302 2302
150 XK 85 Invoice - 2003 2003
150 XL 86 Invoice - 2004 2004
"""


def seed_rows() -> list[tuple[str, str, str, str, str]]:
    rows = []
    for line in SEED.strip().splitlines():
        p = line.split()
        rows.append((p[0], p[1], p[2], " ".join(p[3:-1]), p[-1]))
    return rows


def load_master(db: Session) -> int:
    """Insert the standard rows that are missing (never changes or deletes rows a person added / edited)."""
    have = {(r.client, r.doc_type, r.plant_code) for r in db.scalars(select(DocTypeMaster))}
    n = 0
    for client, dt, rng, desc, plant in seed_rows():
        if (client, dt, plant) not in have:
            db.add(DocTypeMaster(client=client, doc_type=dt, number_range=rng, description=desc, plant_code=plant))
            n += 1
    db.commit()
    return n


def options_for(db: Session, plant: str | None, client: str | None = None) -> list[dict]:
    plant = (plant or "").strip()
    if not plant:
        return []
    q = select(DocTypeMaster).where(DocTypeMaster.plant_code == plant).order_by(DocTypeMaster.doc_type)
    if client:
        q = q.where(DocTypeMaster.client == client)
    return [{"doc_type": r.doc_type, "number_range": r.number_range, "description": r.description}
            for r in db.scalars(q)]


def apply_doc_type(db: Session, h: dict, client: str | None = None) -> None:
    """Set h['doc_type'] from the plant. A value a person chose is kept while it is valid for the plant."""
    opts = options_for(db, h.get("plant_code"), client)
    h["doc_type_options"] = opts
    keys = [o["doc_type"] for o in opts]
    cur = (h.get("doc_type") or "").strip().upper()
    if cur and cur in keys:
        h["doc_type"] = cur
    elif len(keys) == 1:
        h["doc_type"] = keys[0]
        h.setdefault("field_sources", {})["doc_type"] = "MASTER"
    else:
        h["doc_type"] = ""
    # the SAP drop-down shows the DESCRIPTION ("Invoice - R'gaon R1"), so the robot selects by that text
    h["doc_type_desc"] = next((o["description"] or "" for o in opts if o["doc_type"] == h["doc_type"]), "")
