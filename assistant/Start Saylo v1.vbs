Option Explicit

Dim shell, fso, scriptDir, projectDir, pythonExe, entry, widget
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
pythonExe = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
entry = fso.BuildPath(scriptDir, "saylo_background.pyw")
widget = fso.BuildPath(scriptDir, "desktop_widget.pyw")

If Not fso.FileExists(pythonExe) Then
    MsgBox "Saylo's Python environment is missing:" & vbCrLf & pythonExe, vbOKOnly + vbCritical, "Saylo"
    WScript.Quit 1
End If

If MsgBox("Start Saylo with the preserved v1 widget?", vbYesNo + vbQuestion, "Saylo v1") <> vbYes Then WScript.Quit
shell.Run Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & entry & Chr(34), 0, False
shell.Run Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & widget & Chr(34), 0, False
MsgBox "Saylo was asked to start with widget v1.", vbOKOnly + vbInformation, "Saylo v1"
