*** Settings ***
Documentation     KTFL SAP MIRO end-to-end framework - no automatic posting.
Library           ../libraries/DatabaseLibrary.py
Library           ../libraries/InvoiceValidator.py
Library           ../libraries/SAPMIROLibrary.py
Suite Setup       Connect To Invoice Database
Suite Teardown    Disconnect From Invoice Database

*** Test Cases ***
Login And Process Review Invoices
    ${invoices}=    Get Review Invoices
    ${count}=    Get Length    ${invoices}
    Log    REVIEW invoices found: ${count}
    FOR    ${invoice}    IN    @{invoices}
        TRY
            ${result}=    Validate Invoice    ${invoice}
            IF    not ${result}[valid]
                Log    Invalid invoice ${invoice}[id]: ${result}[errors]    WARN
                Update Invoice Status    ${invoice}[id]    REVIEW    ${result}[errors]
            ELSE
                Login Or Attach To Sap
                Open Miro Transaction
                ${fill_result}=    Fill Miro Header And Po    ${invoice}
                IF    ${fill_result}[filled] and ${fill_result}[selected_row]
                    Log    PO/GRN row selected for ${invoice}[invoice_number}. Verify invoice lines and totals in SAP; do not auto-post.
                ELSE
                    Log    Invoice ${invoice}[invoice_number] not ready for posting. Reason: ${fill_result}[reason]    WARN
                END
            END
        EXCEPT    AS    ${error}
            Log    Processing error for invoice id=${invoice}[id]: ${error}    ERROR
            Update Invoice Status    ${invoice}[id]    REVIEW    ${error}
        END
    END
