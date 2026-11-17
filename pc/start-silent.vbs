' VoiceKey 电脑端：静默启动守护进程（无窗口）
' 用法：双击本文件，或把它的快捷方式放进「启动」文件夹（shell:startup）实现开机自启
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

py = dir & "\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then py = "pythonw.exe"

sh.Run """" & py & """ """ & dir & "\supervisor.py""", 0, False
