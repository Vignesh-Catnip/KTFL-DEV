import os
from dotenv import load_dotenv
from libraries.SAPMIROLibrary import SAPMIROLibrary

load_dotenv()

if __name__ == "__main__":
    sap = SAPMIROLibrary()
    sap.login_or_attach_to_sap()
    path = sap.dump_sap_control_tree("reports/sap_control_tree.txt")
    print("SAP control tree saved to:", os.path.abspath(path))
    print("Verify the field/grid IDs and column keys before configuring .env.")
