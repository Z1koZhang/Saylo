Option Explicit

Dim shell, fso, scriptDir, projectDir, pythonExe, entry
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
pythonExe = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
entry = fso.BuildPath(scriptDir, "saylo_supervisor.pyw")

If Not fso.FileExists(pythonExe) Then
    MsgBox "Saylo's Python environment is missing:" & vbCrLf & pythonExe, vbOKOnly + vbCritical, "Saylo"
    WScript.Quit 1
End If

If MsgBox("Start Saylo with the v3.2 realtime widget?", vbYesNo + vbQuestion, "Saylo v3.2") <> vbYes Then WScript.Quit
shell.Run Chr(34) & pythonExe & Chr(34) & " " & Chr(34) & entry & Chr(34) & " --widget desktop_widget_v3.pyw", 0, False
MsgBox "Saylo was asked to start with widget v3.2.", vbOKOnly + vbInformation, "Saylo v3.2"
