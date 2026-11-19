' VoiceKey receiver: start supervisor silently (no window)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "pc"
sh.Run """pc\.venv312\Scripts\pythonw.exe"" ""pc\_supervisor.py""", 0, False
