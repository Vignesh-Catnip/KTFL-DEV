
import win32com.client

try:
    sap = win32com.client.GetObject("SAPGUI")
    app = sap.GetScriptingEngine

    print("SAP GUI connected")
    print("Connections:", app.Children.Count)

    if app.Children.Count == 0:
        print("No active SAP connection. Login to SAP first.")
    else:
        for i in range(app.Children.Count):
            connection = app.Children(i)
            print(f"\nConnection index: {i}")
            print("Sessions:", connection.Children.Count)

            for j in range(connection.Children.Count):
                session = connection.Children(j)
                print("Session index:", j)
                print("Transaction:", session.Info.Transaction)
                print("Screen:", session.Info.ScreenNumber)

except Exception as e:
    print("SAP connection error:", e)
