# charm-kube-virt

Charmhub package name: kubevirt
More information: https://charmhub.io/kubevirt

As an optional addon to charmed-kubernetes, this charm allows for installation of a selectable version of kubevirt into a cluster. KubeVirt allows for virtual machine instances to be launched as kubernetes workloads which interact with other internal services within a kubernetes cluster.

### Basics

* Requires a `charmed-kubernetes` deployment launched by juju
* Deploy the `kubevirt` charm in the model relating to the control-plane and to the worker
* Once the model is active/idle, the kubevirt charm will have successfully deployed KubeVirt into the cluster.
* Confirm the `kubevirt` object is deployed with `kubectl get kubevirt -n kubevirt`

### Troubleshooting
* `kubevirt` requires that the control-plane allows privileged containers.

    ```yaml
    applications:
    kubernetes-control-plane:
      options:
        allow-privileged: "true"
    ```


* If the worker machines are themselves VMs, you will be creating VMs in VMs or (nested virtualization)
  * the charm will enable software-emulation for virtualization if `/dev/kvm` is not detected
    on any one worker node.

* Since KubeVirt `v1.3` the `virt-api` and `virt-controller` deployments, and since `v1.7` the
  `virt-operator` deployment, only schedule onto nodes labelled `node-role.kubernetes.io/control-plane`.
  The `kubernetes-control-plane` charm registers its units with that label by default; keep it in
  its `labels` config option.


## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines
on enhancements to this charm following best practice guidelines, and
[CONTRIBUTING.md](https://github.com/charmed-kubernetes/charm-kube-virt/blob/main/CONTRIBUTING.md)
for developer guidance.


## Installation

Deploying the charm into an existing juju machine model with `charmed-kubernetes` is as simple as 

```bash
juju deploy kubevirt --channel <channel> --config operator-release='<vX.Y.Z>' --trust
```

Next, the charm will need to be related to every `kubernetes-worker` in the model. If you have multiple
kubernetes-worker applications, you may relate the same charm to those multiple applications.

```bash
juju relate kubernetes-worker kubevirt:juju-info
```

Lastly, the charm will need to relate to the control-plane charm in the model.  

```bash
juju relate kubernetes-control-plane kubevirt:kube-control
```

This will install a subordinate unit on every worker, use its cluster credentials to
install the KubeVirt manifests into the cluster, and detect if the related workers 
all have access to an internal `/dev/kvm` device.  The necessary binaries will be installed
on the worker machines using apt (fundamentally `qemu-system-*` or `qemu`) and apparmor
adjustments will be made after the installation. 

### Networking

Within this repository, we will be testing with the default charmed-kubernetes CNI (calico) and will
not be testing with multiple network interfaces per VM instance.  With a stock CNI, your VM will
have access to a single network interface -- the same that is used to communicate with the rest 
of the cluster. Fortunately, not much precludes KubeVirt from accessing other network interfaces
through more advanced CNIs.

See [Usage](#NetworkingUsage) for more details

### Storage

Within this repository, we will be testing with a VMWare cluster which can provider storage through 
the `vsphere-cloud-provider` charm.  This charm can allocate block storage in the VMWare cluster,
mount the block storage into the VM, and use it for persistent storage through a PVC. The very 
same methodology may be used within other clouds with storage classes and PVCs.

## Upgrading

KubeVirt [only supports updating](https://kubevirt.io/user-guide/cluster_admin/updating_and_deletion/)
from the latest patch release of one minor version to the latest patch release of the next
minor version. The charm refuses to apply a release which skips a step of that path and
reports the next release to apply in its `Blocked` status, for example after upgrading the
charm from a revision whose default was `v0.58.0`:

```bash
juju config kubevirt operator-release='v0.58.2'
# wait until the cluster reports the new release as deployed, i.e. until
#   kubectl get kubevirt -n kubevirt kubevirt -o jsonpath='{.status.observedKubeVirtVersion}'
# prints v0.58.2, then continue with the release named by the charm status
juju config kubevirt operator-release='v0.59.2'
...
# once the cluster runs the release before the default, return to the default
juju config kubevirt --reset operator-release
```

`juju run kubevirt/leader list-versions` lists the releases bundled with the charm.


## Usage

Once installed, follow [upstream documenation](https://kubevirt.io/) for complete usage guides.

What to expect:

* a `kubevirt` namespace will be created which contains
  * the replicaset resources: `virt-api`, `virt-controller`, and `virt-operator`
  * the daemonset resource: `virt-handler`
  * the KubeVirt resource: `kubevirt`


Any configuration changes to the `KubeVirt` resource should be considered properies of this charm
though not all have yet been implemented.

* `image-registry`: per upstream, the kubevirt images are hosted on `quay.io`
* `software-emulation`: the charm will detect if all the workers have access to `/dev/kvm`.
  * If true, then `configuration.developerConfiguration.useEmulation` will be `false`
  * If one machine doesn't have `/dev/kvm`, then the charm is forced to enable `useEmulation`
* `pvc-tolerate-less-space-up-to-percent`: is an adjustment necessary under certain circumstances
   where the cloud provider's storage isn't quite sized to create filesystems with enough available space
   see [upstream docs](https://kubevirt.io/user-guide/virtual_machines/disks_and_volumes/#persistentvolumeclaim) for
   more guidance.

### ContainerDisk Images
ContainerDisks are a specialized volume type for Virtual Machine instances.  They consist of a file (`qcow2` or `raw`) in the
`/disk` directory with permissions `o107`.  

See [upstream docs](https://kubevirt.io/user-guide/virtual_machines/disks_and_volumes/#containerdisk) to create your own disk images.

### Networking [NetworkingUsage]

Virtual Machines will have one `pod` interfaces, and can add multiple `multus` network interfaces provided via multus.

Multus can be installed into a kubernetes cluster with the [multus](https://charmhub.io/multus) charm

See the [upstream docs](https://kubevirt.io/user-guide/virtual_machines/interfaces_and_networks/) to determine the
VMI configuration for multiple nics.

### Storage

Virtual Machine instances may be booted with multiple disk devices, each of which can be defined by various kubernetes 
storage components.  See [Supported Volume Types](https://kubevirt.io/user-guide/virtual_machines/disks_and_volumes/#volumes)
for a complete list of volume types.


### VM Management

Creating and Destroying the VM instances is handled through the kubernetes api using `kubectl` commands, but some VM management
takes place using the `virt-api` service in the kubernetes cluster. One may use the `virtctl` client directly
or install a `virtctl` plugin into `kubectl`.  See [upstream docs](https://kubevirt.io/user-guide/operations/virtctl_client_tool/)
for more details on using `virtctl`.  For ease of use this charm can expose `virtctl` through `juju run`

```bash
juju run --unit kubevirt/leader -- ./virtctl help
```
