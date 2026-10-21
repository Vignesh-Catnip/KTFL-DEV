
import win32com.client

sap = win32com.client.GetObject("SAPGUI")
app = sap.GetScriptingEngine

print("Connections:", app.Children.Count)

for i in range(app.Children.Count):
    conn = app.Children(i)
    print("Connection:", i, conn.Description)

    try:
        print("Sessions:", conn.Children.Count)
        for j in range(conn.Children.Count):
            session = conn.Children(j)
            print("Session:", j)
            print("Transaction:", session.Info.Transaction)
            print("Window:", session.ActiveWindow.Text)
    except Exception as e:
        print("Session access error:", e)
