Option Explicit

Dim fso, scriptDir, pidFile, supervisorFile, stopFile, marker
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pidFile = fso.BuildPath(scriptDir, "store\saylo.pid")
supervisorFile = fso.BuildPath(scriptDir, "store\supervisor.pid")
stopFile = fso.BuildPath(scriptDir, "store\saylo.stop")

If Not fso.FileExists(pidFile) And Not fso.FileExists(supervisorFile) Then
    MsgBox "Saylo is not running in the background.", _
           vbOKOnly + vbInformation, "Saylo"
    WScript.Quit
End If

Set marker = fso.CreateTextFile(stopFile, True)
marker.Close
MsgBox "Saylo will stop safely after its current operation finishes.", _
       vbOKOnly + vbInformation, "Saylo"
