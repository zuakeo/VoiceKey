' VoiceKey receiver: start supervisor silently (no window)
' 把本脚本放在 pc/ 目录下双击即可；所有路径按脚本自身位置推导，换机器不用改。
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = here
sh.Run """" & here & "\.venv312\Scripts\pythonw.exe"" """ & here & "\_supervisor.py""", 0, False
