# Installs the uiAccess input relay so SteamlessInput keeps working on
# administrator windows (Task Manager, installers, regedit).
#
# Windows grants the uiAccess privilege only when BOTH are true:
#   1. the exe is Authenticode-signed by a certificate the machine trusts, and
#   2. it runs from a secure location (%ProgramFiles% / %SystemRoot%).
# This script does both. Must run elevated -- writing Program Files and the
# machine certificate store needs administrator rights ONCE, at install time.
# The relay itself never runs elevated: uiAccess is medium integrity, so it
# cannot write Program Files or HKLM. It can only drive the UI.
#
# Run:  powershell -ExecutionPolicy Bypass -File install_uia_relay.ps1
#
# Without this, everything still works -- the app just falls back to lizard
# mode on a Steam Controller / Deck, and to the Steam+View escape elsewhere.

# -NoPause skips the "Press Enter" prompts so this can run unattended (CI, a
# build step, or an installer calling it).
# -ClientExe authorizes a specific SteamlessInput executable to drive the
# relay. Defaults to the app sitting beside this script (or in dist\). The app
# is portable and the relay must live in Program Files, so the two are never in
# the same folder -- this allowlist is how the relay recognises it. Written
# into Program Files, so only an administrator can add to it.
param([switch]$NoPause, [string]$ClientExe)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src  = Join-Path $here "dist\uia-relay"
$dest = Join-Path $env:ProgramFiles "SteamlessInput\uia-relay"
$exe  = Join-Path $dest "SteamlessInputRelay.exe"
$subject = "CN=SteamlessInput Input Relay"

function Fail($msg) {
    Write-Host "`n  ERROR: $msg" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "`n  Press Enter to close" }
    exit 1
}

$admin = ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $admin) { Fail "Run this as administrator (right-click > Run as administrator)." }
if (-not (Test-Path $src)) { Fail "dist\uia-relay not found -- run: python build_uia_relay.py" }

Write-Host "`n  Installing the SteamlessInput input relay...`n" -ForegroundColor Cyan

# 1/4 -- a code-signing certificate the machine trusts.
# Self-signed is fine here: the point of the signature is that WINDOWS can
# verify the binary hasn't been swapped, and a cert in LocalMachine\Root is
# trusted by this machine. (A commercial cert works identically -- if you have
# one, skip this step and sign with that instead.)
Write-Host "  1/4  Certificate..."
$cert = Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.Subject -eq $subject -and $_.NotAfter -gt (Get-Date) } |
        Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject `
            -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(10)
    # Trust it for signature validation, and as a publisher, machine-wide.
    foreach ($store in @("Root", "TrustedPublisher")) {
        $s = New-Object Security.Cryptography.X509Certificates.X509Store($store, "LocalMachine")
        $s.Open("ReadWrite"); $s.Add($cert); $s.Close()
    }
    Write-Host "       created and trusted a new signing certificate"
} else {
    Write-Host "       reusing the existing certificate"
}

# 2/4 -- copy into Program Files (the "secure location" half of the rule).
Write-Host "  2/4  Copying to Program Files..."
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item -Path (Join-Path $src "*") -Destination $dest -Recurse -Force

# 3/4 -- sign it IN PLACE. Signing must happen after the copy: the signature
# covers the file, and the path is what makes it a secure location.
Write-Host "  3/4  Signing..."
$sig = Set-AuthenticodeSignature -FilePath $exe -Certificate $cert `
       -HashAlgorithm SHA256 -ErrorAction Stop
if ($sig.Status -ne "Valid") { Fail "signing failed: $($sig.StatusMessage)" }

# 3b/4 -- authorize the app that may drive the relay.
if (-not $ClientExe) {
    foreach ($cand in @((Join-Path $here "SteamlessInput-windows.exe"),
                        (Join-Path $here "dist\SteamlessInput-windows.exe"))) {
        if (Test-Path $cand) { $ClientExe = (Resolve-Path $cand).Path; break }
    }
}
if ($ClientExe -and (Test-Path $ClientExe)) {
    $ClientExe = (Resolve-Path $ClientExe).Path
    $allow = Join-Path $dest "authorized_clients.txt"
    Set-Content -LiteralPath $allow -Encoding UTF8 -Value @(
        "# Executables allowed to send input through the relay.",
        "# Written by install_uia_relay.ps1; only administrators can edit this.",
        $ClientExe)
    Write-Host "       authorized: $ClientExe"
} else {
    Write-Host "       WARNING: SteamlessInput.exe not found -- re-run with" -ForegroundColor Yellow
    Write-Host "       -ClientExe <path> or the relay will refuse the app." -ForegroundColor Yellow
}

# 4/4 -- verify Windows actually GRANTS the privilege. Everything above can
# succeed and still produce an unprivileged process (wrong store, policy,
# Defender), so confirm the token rather than assuming.
Write-Host "  4/4  Verifying uiAccess..."
$proc = Start-Process $exe -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
$granted = $false
if (-not $proc.HasExited) {
    $code = @'
using System;
using System.Runtime.InteropServices;
public class UIA {
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern IntPtr OpenProcess(int a, bool b, int pid);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern bool OpenProcessToken(IntPtr h, int acc, out IntPtr tok);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern bool GetTokenInformation(IntPtr tok, int cls, out int val, int len, out int ret);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr h);
  public static bool Has(int pid) {
    IntPtr h = OpenProcess(0x1000, false, pid);
    if (h == IntPtr.Zero) return false;
    try {
      IntPtr tok;
      if (!OpenProcessToken(h, 0x0008, out tok)) return false;
      try { int v, r; return GetTokenInformation(tok, 26, out v, 4, out r) && v != 0; }
      finally { CloseHandle(tok); }
    } finally { CloseHandle(h); }
  }
}
'@
    Add-Type -TypeDefinition $code -Language CSharp | Out-Null
    $granted = [UIA]::Has($proc.Id)
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

if ($granted) {
    Write-Host "`n  DONE. The controller now keeps working on administrator" -ForegroundColor Green
    Write-Host "  windows, with your own bindings and the on-screen keyboard." -ForegroundColor Green
} else {
    # Name the actual cause rather than listing suspects. By far the most
    # common one is UAC being switched off: uiAccess is part of the UAC
    # machinery, so with EnableLUA=0 Windows never grants it to anyone.
    $luaKey = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"
    $lua = (Get-ItemProperty $luaKey -Name EnableLUA -ErrorAction SilentlyContinue).EnableLUA
    Write-Host "`n  Installed and signed correctly, but Windows did not grant" -ForegroundColor Yellow
    Write-Host "  uiAccess." -ForegroundColor Yellow
    if ($lua -eq 0) {
        Write-Host "`n  Cause: User Account Control is DISABLED on this PC" -ForegroundColor Cyan
        Write-Host "  (EnableLUA = 0). uiAccess is part of UAC, so nothing can" -ForegroundColor Cyan
        Write-Host "  be granted it while UAC is off." -ForegroundColor Cyan
        Write-Host "`n  Good news: with UAC off you don't need this. Every program" -ForegroundColor Green
        Write-Host "  already runs at high integrity, so nothing is blocked from" -ForegroundColor Green
        Write-Host "  Task Manager in the first place -- the controller keeps" -ForegroundColor Green
        Write-Host "  working there with your normal bindings." -ForegroundColor Green
    } else {
        Write-Host "  The app falls back to lizard mode -- still usable, but a" -ForegroundColor Yellow
        Write-Host "  fixed layout. Check for a policy blocking uiAccess apps." -ForegroundColor Yellow
    }
}
if (-not $NoPause) { Read-Host "`n  Press Enter to close" }
