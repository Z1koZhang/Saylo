Set shell = CreateObject("WScript.Shell")
python = "E:\Anaconda\pythonw.exe"
entry = "H:\Saylo\assistant\saylo_background.pyw"

' Chr(34) is a double quote, keeping both paths safe if they contain spaces.
answer = MsgBox("要启动 Saylo 吗？" & vbCrLf & _
                "它会在后台监听并按现有设置回复微信消息。", _
                vbYesNo + vbQuestion, "启动 Saylo")
If answer <> vbYes Then WScript.Quit

shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & entry & Chr(34), 0, False
MsgBox "已向 Saylo 发出启动请求。" & vbCrLf & _
       "若它已经在运行，不会重复启动。" & vbCrLf & _
       "需要停止时，请双击“停止 Saylo.vbs”。", _
       vbOKOnly + vbInformation, "Saylo"
