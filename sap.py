import win32com.client

sap = win32com.client.GetObject("SAPGUI")
app = sap.GetScriptingEngine

print("Connections:", app.Children.Count)

for i in range(app.Children.Count):
    conn = app.Children(i)

    try:
        print("Connection", i, "Description:", conn.Description)
    except Exception as e:
        print("Description error:", e)

    try:
        print("Sessions:", conn.Children.Count)
    except Exception as e:
        print("Session error:", e)
