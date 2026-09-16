$ErrorActionPreference = 'Stop'

$vmName = 'Saylo-VM'
$networkName = 'SayloNAT'
$gateway = '192.168.250.1'
$prefixLength = 24
$subnet = '192.168.250.0/24'
$adapterAlias = "vEthernet ($networkName)"

if (-not (Get-VM -Name $vmName -ErrorAction SilentlyContinue)) {
    throw "VM $vmName does not exist."
}
if (Get-VMSwitch -Name $networkName -ErrorAction SilentlyContinue) {
    throw "Virtual switch $networkName already exists; refusing to alter it."
}
if (Get-NetNat -Name $networkName -ErrorAction SilentlyContinue) {
    throw "NAT $networkName already exists; refusing to alter it."
}
if (Get-NetIPAddress -IPAddress $gateway -ErrorAction SilentlyContinue) {
    throw "Gateway address $gateway is already in use."
}

New-VMSwitch -Name $networkName -SwitchType Internal | Out-Null
New-NetIPAddress -InterfaceAlias $adapterAlias -IPAddress $gateway -PrefixLength $prefixLength | Out-Null
New-NetNat -Name $networkName -InternalIPInterfaceAddressPrefix $subnet | Out-Null
Connect-VMNetworkAdapter -VMName $vmName -SwitchName $networkName

[PSCustomObject]@{
    vm = $vmName
    switch = $networkName
    switch_type = 'Internal'
    gateway = $gateway
    subnet = $subnet
    nat = $networkName
    inbound_port_forwards = 0
} | ConvertTo-Json | Set-Content -LiteralPath 'H:\Saylo-VM\network-result.json' -Encoding utf8
