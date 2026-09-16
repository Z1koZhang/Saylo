Set fso = CreateObject("Scripting.FileSystemObject")
stopFile = "H:\Saylo\assistant\store\saylo.stop"
pidFile = "H:\Saylo\assistant\store\saylo.pid"

If Not fso.FileExists(pidFile) Then
    MsgBox "Saylo 当前没有在后台运行。", vbOKOnly + vbInformation, "停止 Saylo"
    WScript.Quit
End If

' The background process sees this marker within about half a second and exits cleanly.
Set marker = fso.CreateTextFile(stopFile, True)
marker.Close
MsgBox "已请求停止 Saylo。它会在当前处理完成后安全退出。", _
       vbOKOnly + vbInformation, "停止 Saylo"
