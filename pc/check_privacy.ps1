# Privacy status report (read-only). Run:  .\check_privacy.ps1
#
# Covers the "audio and transcripts are the other privacy risk" review item:
#   - is the system disk encrypted (BitLocker)?
#   - are the data folders readable by other local accounts?
#   - where do the audio / transcripts live, and how much is there?
#
# Nothing is changed here. To fix the ACL findings run:  python harden_acl.py --apply

$pc = "pc"
$py = Join-Path $pc ".venv312\Scripts\python.exe"

Write-Host "== BitLocker ==" -ForegroundColor Cyan
try {
    $v = Get-BitLockerVolume -MountPoint "C:" -ErrorAction Stop
    Write-Host ("  C: protection {0}, encryption {1}%" -f $v.ProtectionStatus, $v.EncryptionPercentage)
    if ($v.ProtectionStatus -ne "On") {
        Write-Host "  WARNING: system disk is not protected - a stolen laptop leaks everything." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  BitLocker status unavailable on this edition / not enabled." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== data location and size ==" -ForegroundColor Cyan
foreach ($d in @("inbox", "failed", "jobs", "out", "quarantine")) {
    $p = Join-Path $pc $d
    if (Test-Path $p) {
        $items = Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue
        $mb = [math]::Round((($items | Measure-Object Length -Sum).Sum) / 1MB, 1)
        Write-Host ("  {0,-12} {1,4} files  {2,7} MB" -f $d, $items.Count, $mb)
    }
}
$rec = Join-Path $pc "inbox\*.m4a"
$oldest = Get-ChildItem $rec -ErrorAction SilentlyContinue | Sort-Object LastWriteTime | Select-Object -First 1
if ($oldest) { Write-Host ("  oldest recording: {0}  ({1:yyyy-MM-dd})" -f $oldest.Name, $oldest.LastWriteTime) }

Write-Host ""
Write-Host "== transcript archive (plain text, by design) ==" -ForegroundColor Cyan
Get-ChildItem (Join-Path $pc "out\*.md") -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 5 |
    ForEach-Object { Write-Host ("  {0}  {1} KB" -f $_.Name, [math]::Round($_.Length / 1KB, 1)) }
Write-Host "  These files hold your full dictation text. Anyone who can read them can read your notes."

Write-Host ""
Write-Host "== folder permissions ==" -ForegroundColor Cyan
& $py (Join-Path $pc "harden_acl.py")

Write-Host ""
Write-Host "== phone side (cannot be checked from here) ==" -ForegroundColor Cyan
Write-Host "  - Android file-based encryption: usually on by default (verify in Settings > Security)."
Write-Host "  - files/audio archive has no cleanup policy yet; recordings stay forever."
