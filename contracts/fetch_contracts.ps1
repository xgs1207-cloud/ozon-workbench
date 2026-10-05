# Fetch the data contracts (JSON Schema) from the upstream project into contracts/original/
#
# Why not raw.githubusercontent: it is unreliable on this network.
# Mirror order: gh-proxy -> jsDelivr -> raw
#
# Usage (Windows PowerShell 5.1 or PowerShell 7):
#   powershell -ExecutionPolicy Bypass -File .\contracts\fetch_contracts.ps1
#   powershell -ExecutionPolicy Bypass -File .\contracts\fetch_contracts.ps1 -OutDir .\.contracts-test
#
# NOTE 1: keep this file ASCII-only. Windows PowerShell 5.1 reads UTF-8 files
#         without a BOM as ANSI, which corrupts non-ASCII comments and can break
#         parsing ("missing closing brace"). Chinese notes live in README.md.
# NOTE 2: do not use $PSScriptRoot in a param default; some hosts evaluate the
#         default before the script scope exists and bind a null path.

[CmdletBinding()]
param(
    [string]$Repo = "jlcglobal/jlc-global-ozon-auto-listing",
    [string]$Ref = "main",
    [string]$OutDir
)

$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    if ($MyInvocation.MyCommand.Path) {
        $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    else {
        $scriptDir = (Get-Location).Path
    }
}
if (-not $OutDir) { $OutDir = Join-Path $scriptDir "original" }

# PowerShell 5.1 may still default to TLS 1.0/1.1
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}
catch {
    # Older or locked-down host: ignore and let the request fail loudly instead.
}

$files = @(
    # M1 capture and analysis
    "templates/collector-capture.schema.json",
    "templates/source.schema.json",
    "templates/source-manifest.schema.json",
    "templates/product-analysis.schema.json",
    "templates/product-positioning.schema.json",
    "templates/category-selection.schema.json",
    # M2 copy
    "templates/ozon-ecommerce-design.schema.json",
    "templates/title-ru.schema.json",
    "templates/description-ru.schema.json",
    "templates/copy-ru.schema.json",
    "templates/keywords-ru.schema.json",
    "templates/keyword-research-ru.schema.json",
    "templates/ozon-tags.schema.json",
    "templates/rich-content.schema.json",
    # M3 images
    "templates/image-plan.schema.json",
    "templates/image-asset-contract.schema.json",
    "templates/image-qc-report.schema.json",
    "templates/ozon-images.schema.json",
    "templates/visual-reference-analysis.schema.json",
    # M4 category / attributes / upload
    "templates/ozon-category.schema.json",
    "templates/ozon-category-tree.schema.json",
    "templates/ozon-category-attributes.schema.json",
    "templates/ozon-attributes.schema.json",
    "templates/ozon-attributes-final.schema.json",
    "templates/ozon-upload-config.schema.json",
    "templates/ozon-upload-payload.schema.json",
    "templates/ozon-upload-preflight.schema.json",
    "templates/ozon-preflight.schema.json",
    "templates/ozon-draft.schema.json",
    "templates/ozon-result.schema.json",
    "templates/store-publications.schema.json",
    "templates/status.schema.json",
    "templates/batch.schema.json",
    "templates/batch-result.schema.json",
    # optional
    "templates/pricing-result.schema.json",
    "templates/cost-analysis.schema.json",
    "templates/profit-analysis.schema.json",
    "templates/variant-grouping-result.schema.json",
    "templates/variant-decision.schema.json",
    "templates/platform-grouping-result.schema.json"
)

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$downloaded = 0
$failed = @()
foreach ($file in $files) {
    $name = Split-Path -Path $file -Leaf
    $target = Join-Path -Path $OutDir -ChildPath $name
    $mirrors = @(
        "https://gh-proxy.com/https://raw.githubusercontent.com/$Repo/$Ref/$file",
        "https://cdn.jsdelivr.net/gh/$Repo@$Ref/$file",
        "https://raw.githubusercontent.com/$Repo/$Ref/$file"
    )
    $ok = $false
    foreach ($url in $mirrors) {
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        try {
            Invoke-WebRequest -Uri $url -OutFile $target -TimeoutSec 90 -UseBasicParsing
            # Validate that it really is JSON before keeping it
            $null = Get-Content -LiteralPath $target -Raw | ConvertFrom-Json
            $ok = $true
            break
        }
        catch {
            # try the next mirror
        }
    }
    if ($ok) { $downloaded++ } else { $failed += $file }
}

Write-Output ("out dir   : " + $OutDir)
Write-Output ("downloaded: " + $downloaded)
if ($failed.Count -gt 0) {
    Write-Output "failed:"
    $failed | ForEach-Object { Write-Output ("  " + $_) }
    Write-Output "Retry the failed ones later, or copy them from GitHub by hand."
    exit 1
}
Write-Output "OK"
