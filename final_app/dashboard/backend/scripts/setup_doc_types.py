"""Creates the document-type master table (and the invoices.doc_type / doc_type_desc columns) in the database from
backend/.env and fills the 18 standard rows.   Run from the backend folder:   python -m scripts.setup_doc_types
(The dashboard does the same automatically when it starts; this is for doing it by hand / checking.)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select                               # noqa: E402

from app import database                                          # noqa: E402
from app.models import DocTypeMaster                              # noqa: E402
from app.services.doctype import load_master                      # noqa: E402

eng = database.get_engine()
added_cols = database.ensure_schema(eng)
print("columns added:", added_cols or "none (already there)")
with database.session_factory()() as db:
    n = load_master(db)
    total = db.scalar(select(func.count()).select_from(DocTypeMaster))
    print(f"Document type master: {n} rows added, {total} rows in total")
    for r in db.scalars(select(DocTypeMaster).order_by(DocTypeMaster.plant_code, DocTypeMaster.doc_type)):
        print(f"  plant {r.plant_code:<5} {r.doc_type:<3} range {r.number_range:<3} {r.description}")
