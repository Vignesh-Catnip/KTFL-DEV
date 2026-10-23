*** Settings ***
Documentation       KTFL - MIRO invoice entry robot
...
...                 Picks invoices with status = MATCHED from PostgreSQL (written by the
...                 invoice extractor) and enters them in MIRO:
...                 1. Basic data: date, reference, Calculate tax, Business Place + Section
...                    (plant code), Text (first line item)
...                 2. PO -> tick ONLY rows whose "Reference Document" = invoice GRN
...                    -> HSN/SAC of those rows = DB HSN (changed in the cell if different)
...                 3. Amount = invoice total -> balance 0? else Details tab: Doc. Type by
...                    plant, Unpl. Del. Csts = balance
...                 4. Simulate -> CHECK: exit | PARK: park | POST: post
...
...                 MIRO_MODE = CHECK (default, nothing saved) | PARK | POST
...                 Run:  run_miro.bat      or      robot --outputdir output tasks\\miro_posting.robot

Library             ../libraries/MiroLibrary.py

Suite Setup         Start Robot
Suite Teardown      Stop Robot


*** Tasks ***
Enter Matched Invoices In MIRO
    ${invoices}=    Get Invoices Ready For MIRO
    IF    not ${invoices}
        Pass Execution    Nothing to do - no MATCHED invoices waiting.
    END
    Connect To SAP
    FOR    ${invoice}    IN    @{invoices}
        Process One Invoice    ${invoice}
    END


*** Keywords ***
Start Robot
    Initialize MIRO Robot
    Connect To Invoice Database

Stop Robot
    Run Keyword And Ignore Error    Reset SAP Screen
    Run Keyword And Ignore Error    Write Run Summary
    Close Connections

Process One Invoice
    [Documentation]    One invoice end-to-end. Any failure is recorded and the loop continues.
    [Arguments]    ${invoice}
    ${claimed}=    Claim Invoice    ${invoice}
    IF    not ${claimed}    RETURN
    TRY
        Open MIRO
        Enter Invoice Header    ${invoice}
        Enter Purchase Order    ${invoice}
        Select GRN Lines    ${invoice}
        Check And Fix HSN    ${invoice}
        Enter Amount And Settle Balance    ${invoice}
        Simulate Invoice    ${invoice}
        ${result}=    Complete Invoice    ${invoice}
        Log    ${invoice}[invoice_number] -> ${result}    console=True
    EXCEPT    AS    ${error}
        ${status}=    Record Failure    ${invoice}    ${error}
        Log    ${invoice}[invoice_number] -> ${status}    console=True
    END
