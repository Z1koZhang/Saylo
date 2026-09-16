$ErrorActionPreference = 'Stop'

$vmName = 'Saylo-VM'
$vmRoot = 'H:\Saylo-VM'
$vhdPath = Join-Path $vmRoot 'Saylo-VM.vhdx'

if (Get-VM -Name $vmName -ErrorAction SilentlyContinue) {
    throw "VM $vmName already exists; refusing to overwrite it."
}
if (Test-Path -LiteralPath $vhdPath) {
    throw "VHD $vhdPath already exists; refusing to overwrite it."
}

New-Item -ItemType Directory -Path $vmRoot -Force | Out-Null
New-VHD -Path $vhdPath -Dynamic -SizeBytes 64GB | Out-Null
New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 4GB -VHDPath $vhdPath -Path $vmRoot | Out-Null
Set-VMProcessor -VMName $vmName -Count 2
Set-VM -Name $vmName -AutomaticCheckpointsEnabled $false

$vm = Get-VM -Name $vmName
$disk = Get-VHD -Path $vhdPath
[PSCustomObject]@{
    name = $vm.Name
    generation = $vm.Generation
    memory_startup_gb = [math]::Round($vm.MemoryStartup / 1GB, 1)
    virtual_cpu = (Get-VMProcessor -VMName $vmName).Count
    vhd_maximum_gb = [math]::Round($disk.Size / 1GB, 0)
    vhd_actual_mb = [math]::Round($disk.FileSize / 1MB, 1)
    vhd_path = $disk.Path
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $vmRoot 'creation-result.json') -Encoding utf8
