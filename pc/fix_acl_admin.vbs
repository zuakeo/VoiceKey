' Double-click this file to repair the data folders' permissions.
' It asks Windows for administrator rights (UAC -> click Yes), then runs
' fix_acl_admin.ps1 in a visible window that stays open when finished.

Option Explicit

Dim fso, shell, base, script, args, rc
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("Shell.Application")

base = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(base, "fix_acl_admin.ps1")

If Not fso.FileExists(script) Then
    MsgBox "fix_acl_admin.ps1 not found next to this file:" & vbCrLf & script, 48, "fix_acl"
    WScript.Quit 1
End If

args = "-NoProfile -ExecutionPolicy Bypass -File """ & script & """"

rc = shell.ShellExecute("powershell.exe", args, base, "runas", 1)

If rc <= 32 Then
    MsgBox "Could not start the elevated window (code " & rc & ")." & vbCrLf & _
           "If you clicked No on the UAC prompt, run it again and click Yes.", 48, "fix_acl"
End If
