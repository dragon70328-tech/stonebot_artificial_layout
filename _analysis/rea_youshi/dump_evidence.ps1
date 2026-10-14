# Dump REA managed-assembly evidence for the GstoneCAD (YouShi CAD) install.
#
# Targets are byte-identical copies of "C:\Program Files (x86)\YouShiCAD\*.dll"
# under .\targets, so every command uses an ASCII path while each REA record
# still carries the original file's SHA-256.
#
# Usage: powershell -ExecutionPolicy Bypass -File _analysis\rea_youshi\dump_evidence.ps1

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$root  = $PSScriptRoot
$tdir  = Join-Path $root 'targets'
$rea   = 'npx.cmd'
$reaArgs = @('-y', 'rea-agents@6.4.0')

# -Force matters: the vendor ships every assembly with the Hidden attribute set,
# and Get-ChildItem -Filter skips hidden files otherwise.
foreach ($f in Get-ChildItem $tdir -Filter *.dll -Force) {
    $safe = $f.BaseName -replace '[^A-Za-z0-9]', '_'
    foreach ($op in 'inspect-managed-artifact', 'inspect-managed-members') {
        $prefix = if ($op -eq 'inspect-managed-artifact') { 'ident' } else { 'members' }
        $out = Join-Path $root ("{0}_{1}.json" -f $prefix, $safe)
        & $rea @reaArgs $op $f.FullName '--format' 'json' 2>&1 | Out-File -FilePath $out -Encoding utf8
        Write-Output ("{0,-10} {1,-38} {2,9}" -f $prefix, $f.Name, (Get-Item $out).Length)
    }
}
