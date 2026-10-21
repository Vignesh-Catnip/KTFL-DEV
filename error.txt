import win32com.client

sap = win32com.client.GetObject("SAPGUI")
app = sap.GetScriptingEngine
connection = app.Children(0)
session = connection.Children(0)

def inspect_controls(parent):
    for i in range(parent.Children.Count):
        control = parent.Children(i)

        try:
            print(
                "ID:", control.Id,
                "| Type:", control.Type,
                "| Name:", control.Name
            )
        except Exception:
            pass

        try:
            inspect_controls(control)
        except Exception:
            pass

inspect_controls(session.FindById("wnd[0]"))
