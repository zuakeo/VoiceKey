# Restrict inbound port 8765 to one device, and verify it really is restricted.
#
# Usage (Administrator):
#   .\restrict_firewall.ps1 -AllowIP 10.0.0.23      # apply
#   .\restrict_firewall.ps1 -Verify                 # check only, no changes
#   .\restrict_firewall.ps1 -Remove                 # drop our rules
#
# Why the old version was not enough:
# "adding an allow rule for the phone" does NOT revoke some other broad allow
# rule that already opens 8765 to everyone. Block rules are evaluated before
# allow rules, so adding a Block is also wrong (it would block the phone too).
# What actually works: the ONLY enabled inbound allow rule covering 8765 must
# be scoped to the phone IP, and every profile must be on with inbound=Block.
#
# If the phone IP changes often, reserve it in the router's DHCP table first --
# otherwise the day the lease changes you will think the service is broken.

param(
    [string]$AllowIP = "",
    [switch]$Verify,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$tag = "InkNote-8765"
$port = 8765

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-PortCovers {
    param($Ports, $P)
    foreach ($x in @($Ports)) {
        if ($null -eq $x) { continue }
        $s = [string]$x
        if ($s -eq "Any" -or $s -eq "*") { return $true }
        if ($s -match '^(\d+)-(\d+)$') {
            if ($P -ge [int]$Matches[1] -and $P -le [int]$Matches[2]) { return $true }
        } elseif ($s -eq "$P") {
            return $true
        }
    }
    return $false
}

function Get-OpenRules {
    # every enabled inbound allow rule that covers our port
    $out = @()
    foreach ($r in Get-NetFirewallRule -ErrorAction SilentlyContinue) {
        if ($r.Enabled -ne 'True' -or $r.Direction -ne 'Inbound' -or $r.Action -ne 'Allow') { continue }
        $pf = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        if (-not $pf) { continue }
        if (-not (Test-PortCovers $pf.LocalPort $port)) { continue }
        $af = $r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
        $remote = @()
        if ($af) { $remote = @($af.RemoteAddress) }
        $wide = $false
        foreach ($a in $remote) {
            if ($null -eq $a) { continue }
            $s = [string]$a
            if ($s -eq "Any" -or $s -eq "*" -or $s -eq "Internet" -or
                $s -eq "LocalSubnet" -or $s -eq "Intranet") { $wide = $true }
        }
        $out += [pscustomobject]@{
            Name   = $r.DisplayName
            Group  = $r.Group
            Wide   = $wide
            Remote = ($remote -join ",")
            Profile = [string]$r.Profile
        }
    }
    return $out
}

if (-not (Test-Admin)) {
    Write-Host "Needs Administrator. Right-click PowerShell -> Run as administrator." -ForegroundColor Yellow
    exit 2
}

# ---------------- verify mode ----------------
if ($Verify) {
    $fail = 0
    Write-Host "== firewall verification ==" -ForegroundColor Cyan

    $svc = Get-Service -Name mpssvc -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Write-Host "  [PASS] firewall service running"
    } else {
        Write-Host "  [FAIL] firewall service not running" -ForegroundColor Red; $fail++
    }

    $profiles = Get-NetFirewallProfile -ErrorAction SilentlyContinue
    foreach ($p in $profiles) {
        if ($p.Enabled -eq 'True') {
            Write-Host "  [PASS] profile $($p.Name) enabled"
        } else {
            Write-Host "  [FAIL] profile $($p.Name) is OFF" -ForegroundColor Red; $fail++
        }
        if ($p.DefaultInboundAction -eq 'Block') {
            Write-Host "  [PASS] $($p.Name) default inbound = Block"
        } else {
            Write-Host "  [FAIL] $($p.Name) default inbound = $($p.DefaultInboundAction) (should be Block)" -ForegroundColor Red
            $fail++
        }
    }

    $open = Get-OpenRules
    $wide = @($open | Where-Object { $_.Wide })
    $narrow = @($open | Where-Object { -not $_.Wide })

    if ($wide.Count -eq 0) {
        Write-Host "  [PASS] no enabled inbound allow rule opens $port to any address"
    } else {
        Write-Host "  [FAIL] these rules still open $port to any address:" -ForegroundColor Red
        $wide | Format-Table Name, Profile, Remote -AutoSize | Out-String | Write-Host
        $fail++
    }

    if ($narrow.Count -gt 0) {
        Write-Host "  [INFO] scoped allow rules covering $port :"
        $narrow | Format-Table Name, Profile, Remote -AutoSize | Out-String | Write-Host
    } else {
        Write-Host "  [WARN] no scoped allow rule found - the phone cannot connect" -ForegroundColor Yellow
    }

    Write-Host ""
    if ($fail -eq 0) {
        Write-Host "RESULT: PASS" -ForegroundColor Green
        exit 0
    }
    Write-Host "RESULT: FAIL ($fail problem(s))" -ForegroundColor Red
    exit 1
}

# ---------------- apply / remove ----------------
Get-NetFirewallRule -Group $tag -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
    Write-Host "Removed rules in group $tag"
    exit 0
}

if (-not $AllowIP) {
    Write-Host "Usage: .\restrict_firewall.ps1 -AllowIP <phone ip>"
    Write-Host "       .\restrict_firewall.ps1 -Verify"
    exit 1
}

# make sure the profiles are actually on, otherwise rules mean nothing
foreach ($p in Get-NetFirewallProfile) {
    if ($p.Enabled -ne 'True') {
        Write-Host "WARNING: profile $($p.Name) is disabled; enabling it" -ForegroundColor Yellow
        Set-NetFirewallProfile -Name $p.Name -Enabled True
    }
}

$active = (Get-NetConnectionProfile -ErrorAction SilentlyContinue | Select-Object -First 1).NetworkCategory
if ($active -and $active -ne "Private") {
    Write-Host "WARNING: current network is '$active', rule below targets Private. Use -Profile any if needed." -ForegroundColor Yellow
}

New-NetFirewallRule -DisplayName "$tag allow phone" -Group $tag -Direction Inbound `
    -Action Allow -Protocol TCP -LocalPort $port -RemoteAddress $AllowIP -Profile Private | Out-Null

Write-Host "Port $port now accepts only $AllowIP (Private profile)."

$open = Get-OpenRules
$wide = @($open | Where-Object { $_.Wide })
if ($wide.Count -gt 0) {
    Write-Host "WARNING: these rules still open $port to any address (other hosts may get in):" -ForegroundColor Yellow
    $wide | Format-Table Name, Profile, Remote -AutoSize | Out-String | Write-Host
    Write-Host "Run with -Verify after fixing them."
} else {
    Write-Host "No other inbound allow rule opens $port to everyone."
}

Get-NetFirewallRule -Group $tag | Select-Object DisplayName, Enabled, Direction, Action |
    Format-Table -AutoSize

Write-Host ""
Write-Host "Next: run '.\restrict_firewall.ps1 -Verify' and confirm RESULT: PASS."
Write-Host "Also test from another LAN device - it must NOT be able to connect."
