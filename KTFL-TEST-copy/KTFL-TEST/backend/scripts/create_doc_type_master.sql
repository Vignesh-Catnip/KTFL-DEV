-- MIRO document type per plant. Run in pgAdmin / psql on database miro_invoice (safe to run more than once).
-- The dashboard also does this by itself when it starts.
CREATE TABLE IF NOT EXISTS doc_type_master (
    id            SERIAL PRIMARY KEY,
    client        VARCHAR(10) DEFAULT '150',
    doc_type      VARCHAR(10) NOT NULL,
    number_range  VARCHAR(10),
    description   VARCHAR(120),
    plant_code    VARCHAR(20) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_type_master_plant_code ON doc_type_master (plant_code);

ALTER TABLE invoices ADD COLUMN IF NOT EXISTS doc_type      VARCHAR(10);      -- e.g. X1
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS doc_type_desc VARCHAR(120);     -- e.g. Invoice - R'gaon R1 (text shown in the SAP drop-down)

INSERT INTO doc_type_master (client, doc_type, number_range, description, plant_code)
SELECT v.client, v.doc_type, v.number_range, v.description, v.plant_code
FROM (VALUES
 ('150','X0','25','Invoice - Mundhwa','2001'),
 ('150','X1','26','Invoice - R''gaon R1','2002'),
 ('150','X2','27','Invoice - Chakan','2020'),
 ('150','X3','28','Invoice - Khed1','2021'),
 ('150','X4','29','Invoice - AMBT1','2022'),
 ('150','X5','30','Invoice - AMBT2','2023'),
 ('150','X6','31','Invoice - KATPHAL','2024'),
 ('150','X7','32','Invoice - Bhiwadi','2025'),
 ('150','X8','33','Invoice-Gujarat','2026'),
 ('150','X9','34','Invoice - Khed2','2027'),
 ('150','XA','35','Invoice - HT Rngn','2081'),
 ('150','XD','71','Invoice - INMT1','2022'),
 ('150','XE','72','Invoice - YIPL1','2301'),
 ('150','XG','81','Invoice - AMBT3','2028'),
 ('150','XH','82','Invoice - 2029','2029'),
 ('150','XI','83','Invoice - 2302','2302'),
 ('150','XK','85','Invoice - 2003','2003'),
 ('150','XL','86','Invoice - 2004','2004')
) AS v(client, doc_type, number_range, description, plant_code)
WHERE NOT EXISTS (SELECT 1 FROM doc_type_master m
                  WHERE m.client = v.client AND m.doc_type = v.doc_type AND m.plant_code = v.plant_code);

SELECT plant_code, doc_type, number_range, description FROM doc_type_master ORDER BY plant_code, doc_type;
