import os
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from robot.api import logger

load_dotenv()

class DatabaseLibrary:
    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self):
        self.conn = None

    def connect_to_invoice_database(self):
        required = ["PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"]
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise RuntimeError("Missing .env settings: " + ", ".join(missing))
        self.conn = psycopg.connect(
            host=os.environ["PGHOST"],
            port=int(os.getenv("PGPORT", "5432")),
            dbname=os.environ["PGDATABASE"],
            user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
            connect_timeout=10,
            row_factory=dict_row,
        )
        logger.info("Connected to invoice PostgreSQL database.")

    def disconnect_from_invoice_database(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def get_review_invoices(self):
        if self.conn is None:
            raise RuntimeError("Database is not connected.")
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'invoices'
            """)
            columns = {r["column_name"] for r in cur.fetchall()}
            required = {"id", "invoice_number", "invoice_date", "po_number", "currency", "status"}
            missing = required - columns
            if missing:
                raise RuntimeError("invoices table missing required columns: " + ", ".join(sorted(missing)))
            optional = ["grn_number"] if "grn_number" in columns else []
            select_cols = ["id", "invoice_number", "invoice_date", "po_number", "currency"] + optional
            sql = "SELECT " + ", ".join('"' + c + '"' for c in select_cols) + " FROM invoices WHERE status = %s ORDER BY id"
            cur.execute(sql, ("REVIEW",))
            rows = cur.fetchall()
        logger.info(f"Loaded {len(rows)} invoice(s) with status REVIEW.")
        if rows and "grn_number" not in rows[0]:
            logger.warn("invoices.grn_number is not available; exact GRN matching cannot be performed from this row.")
        return rows

    def update_invoice_status(self, invoice_id, new_status, message=""):
        if self.conn is None:
            raise RuntimeError("Database is not connected.")
        with self.conn.cursor() as cur:
            cur.execute("UPDATE invoices SET status = %s WHERE id = %s",
                        (str(new_status), int(invoice_id)))
        self.conn.commit()
        logger.info(f"Invoice id={invoice_id} status={new_status}; detail={message}")
