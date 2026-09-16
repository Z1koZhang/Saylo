$ErrorActionPreference = 'Stop'

$vmName = 'Saylo-VM'
$isoPath = 'H:\Saylo-VM\ISO\Windows.iso'

if (-not (Test-Path -LiteralPath $isoPath)) {
    throw "ISO not found: $isoPath"
}
$vm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
if (-not $vm) {
    throw "VM $vmName does not exist."
}

if ($vm.State -ne 'Off') {
    Stop-VM -Name $vmName -TurnOff -Force
}

Set-VMFirmware -VMName $vmName -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
$dvd = Get-VMDvdDrive -VMName $vmName
if (-not $dvd -or $dvd.Path -ne $isoPath) {
    throw "Expected Windows ISO is not attached to the VM DVD drive."
}
Set-VMFirmware -VMName $vmName -FirstBootDevice $dvd
Start-VM -Name $vmName | Out-Null

$firmware = Get-VMFirmware -VMName $vmName
[PSCustomObject]@{
    vm = $vmName
    state = (Get-VM -Name $vmName).State.ToString()
    secure_boot = $firmware.SecureBoot.ToString()
    secure_boot_template = $firmware.SecureBootTemplate
    boot_media = $isoPath
} | ConvertTo-Json | Set-Content -LiteralPath 'H:\Saylo-VM\boot-fix-result.json' -Encoding utf8
