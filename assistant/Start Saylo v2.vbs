Option Explicit

Dim shell, fso, scriptDir, projectDir, pythonExe, entry, widget
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
pythonExe = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
entry = fso.BuildPath(scriptDir, "saylo_background.pyw")
widget = fso.BuildPath(scriptDir, "desktop_widget_v2.pyw")

If Not fso.FileExists(pythonExe) Then
    MsgBox "Saylo's Python environment is missing:" & vbCrLf & pythonExe, vbOKOnly + vbCritical, "Saylo"
    WScript.Quit 1
End If

If MsgBox("Start Saylo with the v2 glass widget?", vbYesNo + vbQuestion, "Saylo v2") <> vbYes Then WScript.Quit
shell.Run Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & entry & Chr(34), 0, False
shell.Run Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & widget & Chr(34), 0, False
MsgBox "Saylo was asked to start with widget v2.", vbOKOnly + vbInformation, "Saylo v2"
