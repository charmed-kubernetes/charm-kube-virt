# charm-kube-virt

Charmhub package name: operator-template
More information: https://charmhub.io/kubevirt

Describe your charm in one or two sentences.

### Details

* Requires a `charmed-kubernetes` deployment launched by juju
* Deploy the `kubevirt` charm in the model relating to the control-plane and to the worker
* Once the model is active/idle, the kubevirt charm will have successfully deployed the KubeVirt into the cluster.
* Confirm the `kubevirt` object is deployed with `kubectl get kubevirt -A`


## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines
on enhancements to this charm following best practice guidelines, and
[CONTRIBUTING.md](https://github.com/charmed-kubernetes/charm-azure-cloud-provider/blob/main/CONTRIBUTING.md)
for developer guidance.
