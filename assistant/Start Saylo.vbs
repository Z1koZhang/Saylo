Option Explicit

Dim shell, fso, scriptDir, projectDir, pythonExe, entry, answer
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
projectDir = fso.GetParentFolderName(scriptDir)
pythonExe = fso.BuildPath(projectDir, ".venv\Scripts\pythonw.exe")
entry = fso.BuildPath(scriptDir, "saylo_supervisor.pyw")

If Not fso.FileExists(pythonExe) Then
    MsgBox "Saylo's Python environment is missing:" & vbCrLf & pythonExe, _
           vbOKOnly + vbCritical, "Saylo"
    WScript.Quit 1
End If

answer = MsgBox("Start Saylo in the background?", _
                vbYesNo + vbQuestion, "Saylo")
If answer <> vbYes Then WScript.Quit

shell.Run Chr(34) & pythonExe & Chr(34) & " " & _
          Chr(34) & entry & Chr(34) & " --widget desktop_widget_v3.pyw", 0, False
MsgBox "Saylo was asked to start." & vbCrLf & _
       "The v3.2 realtime widget should appear in a moment." & vbCrLf & _
       "Right-click it for controls.", _
       vbOKOnly + vbInformation, "Saylo"
