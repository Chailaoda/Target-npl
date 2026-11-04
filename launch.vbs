Set ws = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
currentDir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = currentDir
pythonwPath = currentDir & "\.venv\Scripts\pythonw.exe"
scriptPath = currentDir & "\scripts\start_control.py"

If Not fso.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

ws.Run """" & pythonwPath & """ """ & scriptPath & """ --open", 0, False
