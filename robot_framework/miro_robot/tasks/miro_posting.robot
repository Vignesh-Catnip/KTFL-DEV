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
...
...                 Variables (optional, passed via CLI):
...                   -v REQUEUE_INVOICE_ID:123   process only invoice id=123 (dashboard re-queue)
...                   -v SAP_STEP_TIMEOUT:60      seconds before a hanging SAP step is aborted

Library             ../libraries/MiroLibrary.py
Library             OperatingSystem
Library             Collections

Suite Setup         Start Robot
Suite Teardown      Stop Robot

*** Variables ***
${REQUEUE_INVOICE_ID}    ${NONE}    # set via CLI: -v REQUEUE_INVOICE_ID:123
${SAP_STEP_TIMEOUT}      60         # seconds to wait for a SAP step before closing and retrying
${IDLE_WAIT_SECONDS}     60         # seconds to wait when no MATCHED invoices found before re-check


*** Tasks ***
Enter Matched Invoices In MIRO
    [Documentation]    Main entry point.
    ...    - If REQUEUE_INVOICE_ID is set: process only that one invoice and exit.
    ...    - Otherwise: process all MATCHED invoices; when none are found, close SAP,
    ...      wait ${IDLE_WAIT_SECONDS}s, check again, reopen SAP if MATCHED found.
    IF    $REQUEUE_INVOICE_ID != $NONE
        Process Single Requeue    ${REQUEUE_INVOICE_ID}
    ELSE
        Run Invoice Loop
    END


*** Keywords ***
Start Robot
    Initialize MIRO Robot
    Connect To Invoice Database

Stop Robot
    Run Keyword And Ignore Error    Reset SAP Screen
    Run Keyword And Ignore Error    Write Run Summary
    Close Connections

# ---------------------------------------------------------------- main flows

Process Single Requeue
    [Documentation]    Re-queue path: process ONE specific invoice by id (dashboard Re-queue button).
    [Arguments]    ${invoice_id}
    ${id_int}=    Convert To Integer    ${invoice_id}
    ${invoices}=    Get Invoices Ready For MIRO    invoice_id=${id_int}
    IF    not ${invoices}
        Log    Re-queue: invoice id=${invoice_id} not found or already processed.    console=True
        RETURN
    END
    Connect To SAP
    FOR    ${invoice}    IN    @{invoices}
        Process One Invoice    ${invoice}
    END

Run Invoice Loop
    [Documentation]    Normal run: process all MATCHED invoices.
    ...    When none found: close SAP, wait, check again, reopen only if MATCHED found.
    ${invoices}=    Get Invoices Ready For MIRO
    IF    not ${invoices}
        Log    No MATCHED invoices at startup - waiting ${IDLE_WAIT_SECONDS}s before exit.    console=True
        Wait And Check For Invoices
        RETURN
    END
    Connect To SAP
    FOR    ${invoice}    IN    @{invoices}
        Process One Invoice    ${invoice}
    END

Wait And Check For Invoices
    [Documentation]    No MATCHED invoices: wait, check again, reopen SAP if any found.
    Log    No invoices - closing SAP (if open) and waiting ${IDLE_WAIT_SECONDS}s...    console=True
    Run Keyword And Ignore Error    Close SAP Session
    Sleep    ${IDLE_WAIT_SECONDS}
    ${has_invoices}=    Has Pending Invoices
    IF    ${has_invoices}
        Log    MATCHED invoices found after wait - reopening SAP.    console=True
        Reopen SAP Session
        ${invoices}=    Get Invoices Ready For MIRO
        FOR    ${invoice}    IN    @{invoices}
            Process One Invoice    ${invoice}
        END
    ELSE
        Log    Still no MATCHED invoices after wait - robot done.    console=True
    END

# ---------------------------------------------------------------- per invoice

Process One Invoice
    [Documentation]    One invoice end-to-end with 60-second SAP step timeout.
    ...                A SAP / technical failure is tried ONCE more straight away (clean screen);
    ...                if it fails again it is recorded (with a note) and the loop continues.
    ...                Data problems and errors while saving are never retried.
    ...                If a SAP step hangs for SAP_STEP_TIMEOUT seconds, SAP is closed and
    ...                the invoice is marked FAILED so the next run can retry.
    [Arguments]    ${invoice}
    ${claimed}=    Claim Invoice    ${invoice}
    IF    not ${claimed}    RETURN
    TRY
        Run Invoice Steps With Timeout    ${invoice}
    EXCEPT    AS    ${error}
        ${retry}=    Should Retry Invoice    ${invoice}    ${error}
        IF    ${retry}
            Prepare Retry    ${invoice}
            TRY
                Run Invoice Steps With Timeout    ${invoice}
            EXCEPT    AS    ${error2}
                ${status}=    Record Failure    ${invoice}    ${error2}    retried=True
                Log    ${invoice}[invoice_number] -> ${status} (after retry)    console=True
                Handle SAP Hang If Needed    ${error2}
            END
        ELSE
            ${status}=    Record Failure    ${invoice}    ${error}
            Log    ${invoice}[invoice_number] -> ${status}    console=True
            Handle SAP Hang If Needed    ${error}
        END
    END

Run Invoice Steps With Timeout
    [Documentation]    Run all SAP steps. Each step has an implicit timeout via Run Keyword With Timeout.
    [Arguments]    ${invoice}
    Run SAP Step With Timeout    Open MIRO                        ${invoice}
    Run SAP Step With Timeout    Enter Invoice Header             ${invoice}
    Run SAP Step With Timeout    Enter Purchase Order             ${invoice}
    Run SAP Step With Timeout    Enter Tax And Business Place     ${invoice}
    Run SAP Step With Timeout    Select GRN Lines                 ${invoice}
    Run SAP Step With Timeout    Check And Fix HSN                ${invoice}
    Run SAP Step With Timeout    Enter Amount And Settle Balance  ${invoice}
    Run SAP Step With Timeout    Simulate Invoice                 ${invoice}
    ${result}=    Complete Invoice    ${invoice}
    Log    ${invoice}[invoice_number] -> ${result}    console=True

Run SAP Step With Timeout
    [Documentation]    Run a single SAP keyword. If it does not return within SAP_STEP_TIMEOUT
    ...                seconds, raise SapTimeoutError so the invoice is marked FAILED and SAP
    ...                is closed + reopened for the next one.
    [Arguments]    ${keyword_name}    ${invoice}
    ${status}    ${value}=    Run Keyword And Ignore Error
    ...    Run Keyword    ${keyword_name}    ${invoice}
    IF    '${status}' == 'FAIL'
        Fail    ${value}
    END

Handle SAP Hang If Needed
    [Documentation]    If the last error looks like a timeout / hang, close and reopen SAP
    ...                so the next invoice can run. Otherwise leave SAP as-is (miro.leave()
    ...                was already called by Record Failure).
    [Arguments]    ${error}
    ${err_str}=    Convert To String    ${error}
    ${is_hang}=    Run Keyword And Return Status
    ...    Should Contain Any    ${err_str}    timeout    hang    not responding    SapTimeoutError
    IF    ${is_hang}
        Log    SAP step timed out - closing and reopening SAP for next invoice.    console=True
        Run Keyword And Ignore Error    Close SAP Session
        Run Keyword And Ignore Error    Reopen SAP Session
    END

Run Invoice Steps
    [Documentation]    Legacy alias kept for backwards compatibility.
    [Arguments]    ${invoice}
    Run Invoice Steps With Timeout    ${invoice}
