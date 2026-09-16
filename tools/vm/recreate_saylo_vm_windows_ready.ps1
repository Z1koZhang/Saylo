$ErrorActionPreference = 'Stop'

$vmName = 'Saylo-VM'
$vmRoot = 'H:\Saylo-VM'
$vhdPath = Join-Path $vmRoot 'Saylo-VM.vhdx'
$isoPath = 'H:\Saylo-VM\ISO\Windows.iso'
$switchName = 'SayloNAT'
$maxEmptyVhdBytes = 32MB

if (-not (Test-Path -LiteralPath $isoPath)) {
    throw "ISO not found: $isoPath"
}
if (-not (Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue)) {
    throw "Required NAT switch $switchName does not exist."
}

$oldVm = Get-VM -Name $vmName -ErrorAction SilentlyContinue
if ($oldVm) {
    $oldDisk = Get-VMHardDiskDrive -VMName $vmName
    if ($oldDisk.Count -ne 1 -or $oldDisk[0].Path -ne $vhdPath) {
        throw "Existing VM disk layout is unexpected; refusing to remove it."
    }
    $oldVhd = Get-VHD -Path $vhdPath
    if ($oldVhd.FileSize -gt $maxEmptyVhdBytes) {
        throw "Existing VHD is not empty; refusing to remove it."
    }
    if ($oldVm.State -ne 'Off') {
        Stop-VM -Name $vmName -TurnOff -Force
    }
    Remove-VM -Name $vmName -Force
}

if (Test-Path -LiteralPath $vhdPath) {
    $oldVhd = Get-VHD -Path $vhdPath
    if ($oldVhd.FileSize -gt $maxEmptyVhdBytes) {
        throw "Existing VHD is not empty; refusing to remove it."
    }
    Remove-Item -LiteralPath $vhdPath -Force
}

New-VHD -Path $vhdPath -Dynamic -SizeBytes 64GB | Out-Null
New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 4GB -VHDPath $vhdPath -Path $vmRoot | Out-Null
Set-VMProcessor -VMName $vmName -Count 2
Set-VM -Name $vmName -AutomaticCheckpointsEnabled $false
Set-VMFirmware -VMName $vmName -EnableSecureBoot On -SecureBootTemplate MicrosoftWindows
Connect-VMNetworkAdapter -VMName $vmName -SwitchName $switchName
Set-VMKeyProtector -VMName $vmName -NewLocalKeyProtector | Out-Null
Enable-VMTPM -VMName $vmName
Add-VMDvdDrive -VMName $vmName -Path $isoPath | Out-Null
$dvd = Get-VMDvdDrive -VMName $vmName
Set-VMFirmware -VMName $vmName -FirstBootDevice $dvd
Start-VM -Name $vmName | Out-Null

$vm = Get-VM -Name $vmName
$disk = Get-VHD -Path $vhdPath
$firmware = Get-VMFirmware -VMName $vmName
$security = Get-VMSecurity -VMName $vmName
[PSCustomObject]@{
    vm = $vm.Name
    state = $vm.State.ToString()
    generation = $vm.Generation
    memory_startup_gb = [math]::Round($vm.MemoryStartup / 1GB, 1)
    virtual_cpu = (Get-VMProcessor -VMName $vmName).Count
    vhd_maximum_gb = [math]::Round($disk.Size / 1GB, 0)
    vhd_actual_mb = [math]::Round($disk.FileSize / 1MB, 1)
    secure_boot = $firmware.SecureBoot.ToString()
    secure_boot_template = $firmware.SecureBootTemplate
    tpm_enabled = $security.TpmEnabled
    network_switch = $switchName
    boot_media = $isoPath
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $vmRoot 'recreate-result.json') -Encoding utf8
