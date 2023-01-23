# charm-kube-virt

Charmhub package name: kubevirt
More information: https://charmhub.io/kubevirt

As an optional addon to charmed-kubernetes, this charm allows for installation of a selectable version of kubevirt into a cluster. KubeVirt allows for virtual machine instances to be launched as kubernetes workloads which interact with other internal services within a kubernetes cluster.

### Details

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


## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines
on enhancements to this charm following best practice guidelines, and
[CONTRIBUTING.md](https://github.com/charmed-kubernetes/charm-azure-cloud-provider/blob/main/CONTRIBUTING.md)
for developer guidance.
