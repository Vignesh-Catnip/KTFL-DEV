import win32com.client

sap = win32com.client.GetObject("SAPGUI")
app = sap.GetScriptingEngine

print("Connections:", app.Children.Count)

for i in range(app.Children.Count):
    conn = app.Children(i)
    print("Connection:", i, conn.Description)
    print("Sessions:", conn.Children.Count)
