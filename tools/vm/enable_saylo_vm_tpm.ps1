$ErrorActionPreference = 'Stop'

$vmName = 'Saylo-VM'
if (-not (Get-VM -Name $vmName -ErrorAction SilentlyContinue)) {
    throw "VM $vmName does not exist."
}

$firmware = Get-VMFirmware -VMName $vmName
if (-not $firmware.SecureBoot) {
    Set-VMFirmware -VMName $vmName -EnableSecureBoot On
}

$security = Get-VMSecurity -VMName $vmName
if (-not $security.TpmEnabled) {
    Set-VMKeyProtector -VMName $vmName -NewLocalKeyProtector | Out-Null
    Enable-VMTPM -VMName $vmName
}

$firmware = Get-VMFirmware -VMName $vmName
$security = Get-VMSecurity -VMName $vmName
[PSCustomObject]@{
    vm = $vmName
    secure_boot = $firmware.SecureBoot
    tpm_enabled = $security.TpmEnabled
} | ConvertTo-Json | Set-Content -LiteralPath 'H:\Saylo-VM\security-result.json' -Encoding utf8
