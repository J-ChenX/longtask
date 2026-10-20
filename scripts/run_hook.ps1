# platform-adapters: native hook consumer; binding content is data, never shell code.
$ErrorActionPreference = 'Stop'
if (-not $env:PLUGIN_DATA) { throw 'Missing Codex PLUGIN_DATA for Python binding' }
$binding = Join-Path $env:PLUGIN_DATA 'python-path'
if (-not (Test-Path -LiteralPath $binding)) {
    throw "Bind a verified Python 3.14+ executable at: $binding (UTF-8, one absolute path, LF terminated)"
}
$info = Get-Item -LiteralPath $binding -Force
if ($info -isnot [IO.FileInfo] -or ($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $info.Length -gt 4096) {
    throw 'Unsafe longtask Python binding'
}
$raw = [IO.File]::ReadAllText($binding, [Text.UTF8Encoding]::new($false, $true))
if (-not $raw.EndsWith("`n")) { throw 'Expected one LF-terminated absolute Python path' }
$python = $raw.Substring(0, $raw.Length - 1)
if ($python -match '[\x00-\x1f]' -or $python -notmatch '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/])') {
    throw 'Expected one LF-terminated absolute Python path'
}
& $python -E -s -B (Join-Path $PSScriptRoot 'longtask_hooks.py')
if ($null -eq $LASTEXITCODE) { exit 1 }
exit $LASTEXITCODE
