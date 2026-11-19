# Repair the data folders' file permissions (needs Administrator).
#
# Run fix_acl_admin.bat (double-click) instead of this file: the .bat elevates
# itself and keeps the window open so you can read the result.
#
# Why: `icacls <dir> /inheritance:r /grant "user:(OI)(CI)F" /T` was run on these
# folders. (OI)(CI) means nothing for a file, and /T also stripped the inherited
# ACEs, so every existing file ended up with an EMPTY ACL: even the owner cannot
# read it without elevation. Contents are intact - only permissions need rebuilding.
#
# Step 1  icacls <dir> /reset /T      -> files inherit normal permissions again
# Step 2  python harden_acl.py --apply -> tighten again, per file with (F)
#
# This script deliberately does NOT stop on the first error and always writes a
# log and pauses, because a window that closes instantly tells you nothing.

param([switch]$NoPause)

$pc  = "pc"
$py  = Join-Path $pc ".venv312\Scripts\python.exe"
$log = Join-Path $pc "out\fix_acl.log"

function Write-Log {
    param([string]$Text, [string]$Color = "Gray")
    Write-Host $Text -ForegroundColor $Color
    try { Add-Content -Path $log -Value $Text -Encoding UTF8 } catch { }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

try { New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null } catch { }

if (-not (Test-Admin)) {
    Write-Host "Not elevated - requesting administrator permission..." -ForegroundColor Yellow
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($NoPause) { $argList += " -NoPause" }
    Start-Process -FilePath "powershell.exe" -ArgumentList $argList -Verb RunAs
    exit
}

Write-Log ("== fix_acl  " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss") + "  user=" + $env:USERNAME) "Cyan"
Write-Log ("admin=" + (Test-Admin))

Write-Log "== step 1/2: reset inheritance ==" "Cyan"
foreach ($d in @("inbox", "failed", "jobs", "out", "quarantine")) {
    $p = Join-Path $pc $d
    if (-not (Test-Path $p)) { Write-Log ("  skip {0} (missing)" -f $d); continue }
    $out = & icacls $p /reset /T /Q 2>&1
    Write-Log ("  reset {0}  exit={1}" -f $d, $LASTEXITCODE)
    if ($LASTEXITCODE -ne 0) { Write-Log ("    " + ($out | Out-String).Trim()) "Yellow" }
}

Write-Log "== step 2/2: tighten per file ==" "Cyan"
if (-not (Test-Path $py)) {
    Write-Log ("  python not found: " + $py) "Red"
} else {
    $out = & $py (Join-Path $pc "harden_acl.py") --apply 2>&1
    $out | ForEach-Object { Write-Log ("  " + $_) }
}

Write-Log "== spot check ==" "Cyan"
foreach ($f in @("out\receiver.log", "jobs", "inbox\rec_20261116_203516.m4a")) {
    $p = Join-Path $pc $f
    if (Test-Path $p) {
        try {
            $a = Get-Acl $p
            Write-Log ("  {0}: {1} ACE(s), owner {2}" -f $f, $a.Access.Count, $a.Owner)
        } catch {
            Write-Log ("  {0}: Get-Acl failed - {1}" -f $f, $_.Exception.Message) "Yellow"
        }
    }
}

Write-Log ""
Write-Log "If every line above shows at least 1 ACE, the permissions are repaired." "Green"
Write-Log ("Log file: " + $log)

# This log was created while elevated, so it carries a higher integrity level and
# cannot be tightened later from a normal process. Do it here, while we still can.
try {
    & icacls $log /inheritance:r /grant:r "$($env:USERNAME):(F)" `
        /grant:r "SYSTEM:(F)" /grant:r "Administrators:(F)" 2>&1 | Out-Null
} catch { }

if (-not $NoPause) { Read-Host "Press Enter to close" | Out-Null }
