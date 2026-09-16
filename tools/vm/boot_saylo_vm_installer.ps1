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
    throw "VM $vmName must be off before changing boot media."
}

$dvd = Get-VMDvdDrive -VMName $vmName -ErrorAction SilentlyContinue
if ($dvd) {
    if ($dvd.Path -and $dvd.Path -ne $isoPath) {
        throw "A different ISO is already attached: $($dvd.Path)"
    }
    Set-VMDvdDrive -VMName $vmName -ControllerNumber $dvd.ControllerNumber -ControllerLocation $dvd.ControllerLocation -Path $isoPath
} else {
    Add-VMDvdDrive -VMName $vmName -Path $isoPath | Out-Null
}

$dvd = Get-VMDvdDrive -VMName $vmName
Set-VMFirmware -VMName $vmName -FirstBootDevice $dvd
Start-VM -Name $vmName | Out-Null

[PSCustomObject]@{
    vm = $vmName
    state = (Get-VM -Name $vmName).State.ToString()
    boot_media = $isoPath
    boot_device = 'DVD'
} | ConvertTo-Json | Set-Content -LiteralPath 'H:\Saylo-VM\boot-result.json' -Encoding utf8
