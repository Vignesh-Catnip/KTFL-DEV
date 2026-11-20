*** Settings ***
Documentation       KTFL - MIRO invoice entry robot
...
...                 Picks invoices with status = MATCHED from PostgreSQL (written by the
...                 invoice extractor) and enters them in MIRO:
...                 1. Basic data: date, reference -> PO -> then Calculate tax, Tax code
...                    (invoices.tax_code), Business Place + Section, Text (first line item)
...                 2. Tick ONLY rows whose "Reference Document" = invoice GRN
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
    [Documentation]    One invoice end-to-end. A SAP / technical failure is tried ONCE more straight away (clean screen);
    ...                if it fails again it is recorded (with a note) and the loop continues. Data problems and errors
    ...                while saving are never retried (a second try could not help / could post twice).
    [Arguments]    ${invoice}
    ${claimed}=    Claim Invoice    ${invoice}
    IF    not ${claimed}    RETURN
    TRY
        Run Invoice Steps    ${invoice}
    EXCEPT    AS    ${error}
        ${retry}=    Should Retry Invoice    ${invoice}    ${error}
        IF    ${retry}
            Prepare Retry    ${invoice}
            TRY
                Run Invoice Steps    ${invoice}
            EXCEPT    AS    ${error2}
                ${status}=    Record Failure    ${invoice}    ${error2}    retried=True
                Log    ${invoice}[invoice_number] -> ${status} (after retry)    console=True
            END
        ELSE
            ${status}=    Record Failure    ${invoice}    ${error}
            Log    ${invoice}[invoice_number] -> ${status}    console=True
        END
    END

Run Invoice Steps
    [Arguments]    ${invoice}
    Open MIRO
    Enter Invoice Header    ${invoice}
    Enter Purchase Order    ${invoice}
    Enter Tax And Business Place    ${invoice}
    Select GRN Lines    ${invoice}
    Check And Fix HSN    ${invoice}
    Enter Amount And Settle Balance    ${invoice}
    Simulate Invoice    ${invoice}
    ${result}=    Complete Invoice    ${invoice}
    Log    ${invoice}[invoice_number] -> ${result}    console=True
